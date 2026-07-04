# ai_monitor.py
# Local AI System Monitor — gemma4:e2b via Ollama
# Features: system metrics + Windows Event Log + responsive popup + manual prompt

import ollama
import psutil
import time
import datetime
import os
import sys
import threading
import subprocess
import json
import tempfile

# ── Config ────────────────────────────────────────────────────────────────────
# Dynamically determine the script directory to handle relative log path placement
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
LOG_FILE       = os.path.join(SCRIPT_DIR, "ai_monitor_log.txt")
MODEL          = 'gemma4:e2b'
INTERVAL       = 30   # Time in seconds between automated monitoring sweeps
POPUP_EVERY_N  = 3    # Trigger desktop popup window every N cycles (3 x 30s = 90s)
EVENT_LOOKBACK = 5    # Time window size in minutes to scan the Windows Event Logs
cycle_count    = 0

# ── Logging ───────────────────────────────────────────────────────────────────
def log(message):
    """Formats, prints, and appends timestamped operational text messages into the local log file."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line      = f"[{timestamp}] {message}"
    print(line, flush=True)  # Instantly flush console outputs
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()  # Force OS file-write operations down to disk immediately
    except Exception as e:
        print(f"[LOG ERROR] Cannot write to file: {e}", flush=True)

def log_startup():
    """Validates log file write accessibility on startup and prints out systemic metadata blocks."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("")
        print(f">> Log file OK: {LOG_FILE}", flush=True)
    except Exception as e:
        print(f">> WARNING: Cannot write log file — {e}", flush=True)
        print(f">> Attempted path: {LOG_FILE}", flush=True)

    log("=" * 60)
    log("  AI System Monitor started")
    log(f"  Script dir : {SCRIPT_DIR}")
    log(f"  Log file   : {LOG_FILE}")
    log(f"  Model      : {MODEL}")
    log(f"  Interval   : every {INTERVAL}s")
    log(f"  Popup      : every {POPUP_EVERY_N} cycles ({POPUP_EVERY_N * INTERVAL}s)")
    log("=" * 60)

# ── System metrics ────────────────────────────────────────────────────────────
def get_system_stats():
    """Collects real-time hardware resource consumption using psutil and normalizes bytes to GB."""
    cpu  = psutil.cpu_percent(interval=1) # Blocking call for 1 sec to sample accurate CPU load
    ram  = psutil.virtual_memory()
    disk = psutil.disk_usage('C:\\')
    return {
        "cpu_percent":  cpu,
        "ram_used_gb":  round(ram.used  / (1024**3), 2),
        "ram_total_gb": round(ram.total / (1024**3), 2),
        "ram_percent":  ram.percent,
        "disk_used_gb": round(disk.used  / (1024**3), 2),
        "disk_total_gb":round(disk.total / (1024**3), 2),
        "disk_percent": disk.percent,
    }

# ── Windows Event Log ─────────────────────────────────────────────────────────
def get_windows_events():
    """Invokes PowerShell via a subprocess pipeline to read recent System/Application Errors (Levels 1,2)."""
    ps_command = f"""
$results = @()
foreach ($logName in @('System', 'Application')) {{
    try {{
        # Extract the 10 most recent Critical (1) and Error (2) events within the lookback window
        $ev = Get-WinEvent -FilterHashtable @{{
            LogName   = $logName
            Level     = 1,2
            StartTime = (Get-Date).AddMinutes(-{EVENT_LOOKBACK})
        }} -MaxEvents 10 -ErrorAction SilentlyContinue
        if ($ev) {{ $results += $ev }}
    }} catch {{}}
}}
if ($results.Count -gt 0) {{
    # Sort chronologically, pull the top 8 entries, and truncate long message strings to optimize prompt size
    $results | Sort-Object TimeCreated -Descending | Select-Object -First 8 |
    Select-Object TimeCreated, Id, LevelDisplayName, LogName,
      @{{n='Message'; e={{ $_.Message.Substring(0, [Math]::Min(150, $_.Message.Length)) }}}} |
    ConvertTo-Json -Compress
}} else {{
    '[]'
}}
"""
    try:
        # Spawn the PowerShell execution bypass process safely without terminal rendering
        result = subprocess.run(
            ['powershell', '-ExecutionPolicy', 'Bypass', '-Command', ps_command],
            capture_output=True, text=True, timeout=15
        )
        output = result.stdout.strip()
        if not output or output == '[]':
            return []
        # Wrap single JSON objects into an iterable list container to prevent parsing mismatches
        if output.startswith('{'):
            output = f'[{output}]'
        parsed = json.loads(output)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception as e:
        return [{"fetch_error": str(e)}]

def format_events_for_prompt(events):
    """Assembles a cleanly indented plain-text block of event details to inject into the LLM system prompt."""
    if not events:
        return "None — no Critical or Error events in the last 5 minutes."
    lines = []
    for e in events:
        if "fetch_error" in e:
            lines.append(f"  [Could not read event log: {e['fetch_error']}]")
            continue
        lines.append(
            f"  [{e.get('LevelDisplayName','?')}] "
            f"Log:{e.get('LogName','?')}  "
            f"ID:{e.get('Id','?')}  "
            f"— {e.get('Message','')[:130]}"
        )
    return "\n".join(lines)

# ── Auto AI analysis ──────────────────────────────────────────────────────────
def ask_ai(stats, events):
    """Structures hardware metrics and OS error traces into a unified text prompt for the local Gemma model."""
    event_text = format_events_for_prompt(events)
    prompt = (
        f"Windows PC health report:\n\n"
        f"SYSTEM METRICS:\n"
        f"  CPU  : {stats['cpu_percent']}%\n"
        f"  RAM  : {stats['ram_used_gb']}GB / {stats['ram_total_gb']}GB ({stats['ram_percent']}%)\n"
        f"  Disk : {stats['disk_used_gb']}GB / {stats['disk_total_gb']}GB ({stats['disk_percent']}%)\n\n"
        f"WINDOWS EVENT LOG — Critical and Error only (last {EVENT_LOOKBACK} min):\n"
        f"{event_text}\n\n"
        f"In 2-3 sentences: summarize overall health, highlight any concerning "
        f"Event IDs, and give one concrete action the user should take."
    )
    response = ""
    # Process the chat completion via local Ollama socket streaming
    stream = ollama.chat(
        model=MODEL,
        messages=[{'role': 'user', 'content': prompt}],
        stream=True,
    )
    for chunk in stream:
        response += chunk['message']['content']
    return response.strip()

# ── Manual AI prompt ──────────────────────────────────────────────────────────
def ask_ai_custom(question):
    """Handles independent user questions from the interactive shell thread using raw text routing."""
    response = ""
    stream = ollama.chat(
        model=MODEL,
        messages=[{'role': 'user', 'content': question}],
        stream=True,
    )
    for chunk in stream:
        response += chunk['message']['content']
    return response.strip()

# ── Popup — PowerShell Windows Forms (responsive, no pip install needed) ──────
def show_popup(stats, events, ai_response):
    """Generates a graphical desktop overlay UI using integrated .NET Windows Forms via background processes."""
    try:
        real_events  = [e for e in events if "fetch_error" not in e]
        count        = len(real_events)
        event_status = f"{count} issue(s) detected" if count > 0 else "No issues found"
        event_lines  = "\n".join([
            f"[{e.get('LevelDisplayName','?')}] ID {e.get('Id','?')} "
            f"({e.get('LogName','?')}): {e.get('Message','')[:120]}"
            for e in real_events[:5]
        ]) if real_events else "No Critical or Error events in the last 5 minutes."

        # Serialize system metrics out to a secure temporary JSON payload to prevent cmdline arg truncation
        popup_data = {
            "cpu":          stats['cpu_percent'],
            "ram":          stats['ram_percent'],
            "ram_used":     stats['ram_used_gb'],
            "ram_total":    stats['ram_total_gb'],
            "disk":         stats['disk_percent'],
            "disk_used":    stats['disk_used_gb'],
            "disk_total":   stats['disk_total_gb'],
            "event_count":  count,
            "event_status": event_status,
            "event_lines":  event_lines,
            "ai_response":  ai_response,
            "timestamp":    datetime.datetime.now().strftime("%Y-%m-%d  %H:%M:%S"),
        }

        tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False, encoding='utf-8'
        )
        json.dump(popup_data, tmp, ensure_ascii=False)
        tmp.close()
        tmp_path = tmp.name.replace('\\', '\\\\')

        # Complex multi-line PowerShell script defining the Windows Forms layouts, tables, and colors
        ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$raw  = Get-Content -Path '{tmp_path}' -Raw -Encoding UTF8
$d    = $raw | ConvertFrom-Json
Remove-Item '{tmp_path}' -Force -ErrorAction SilentlyContinue

# ── Helper: color code metrics dynamically based on percentage threshold entries ────
function ColorLabel($val) {{
    if ($val -ge 90) {{ return [System.Drawing.Color]::FromArgb(255, 80, 80) }}   # Crimson Warning
    if ($val -ge 70) {{ return [System.Drawing.Color]::FromArgb(255, 165, 0) }}  # Amber Alert
    return [System.Drawing.Color]::FromArgb(80, 200, 80)                         # Stable Green
}}

# ── Base Form Canvas Configuration ───────────────────────────────────────────
$form                  = New-Object System.Windows.Forms.Form
$form.Text             = "PC Health Monitor"
$form.Size             = New-Object System.Drawing.Size(580, 600)
$form.MinimumSize      = New-Object System.Drawing.Size(480, 500)
$form.StartPosition    = "CenterScreen"
$form.TopMost          = $true
$form.BackColor        = [System.Drawing.Color]::FromArgb(30, 30, 30) # Dark Theme background
$form.ForeColor        = [System.Drawing.Color]::White
$form.Font             = New-Object System.Drawing.Font("Segoe UI", 9)
$form.Padding          = New-Object System.Windows.Forms.Padding(14)

# ── Table Layout Manager ─────────────────────────────────────────────────────
$layout                = New-Object System.Windows.Forms.TableLayoutPanel
$layout.Dock           = "Fill"
$layout.ColumnCount    = 1
$layout.RowCount       = 6
$layout.Padding        = New-Object System.Windows.Forms.Padding(12)
$layout.BackColor      = [System.Drawing.Color]::FromArgb(30, 30, 30)
[void]$layout.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle("Percent", 100)))
[void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("AutoSize")))  # Row 0: Timestamp Title
[void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("AutoSize")))  # Row 1: Section Header
[void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("AutoSize")))  # Row 2: Flow Metrics Cards
[void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("AutoSize")))  # Row 3: Windows Event Terminal
[void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("Percent", 100)))  # Row 4: AI Analysis Block (Fills space)
[void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle("AutoSize")))  # Row 5: Interaction Controls
$form.Controls.Add($layout)

# ── Header Title Control ────────────────────────────────────────────────────
$lblTitle           = New-Object System.Windows.Forms.Label
$lblTitle.Text      = "PC Health Summary   —   $($d.timestamp)"
$lblTitle.Font      = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$lblTitle.AutoSize  = $false
$lblTitle.Dock      = "Fill"
$lblTitle.Height    = 36
$lblTitle.ForeColor = [System.Drawing.Color]::White
$layout.Controls.Add($lblTitle, 0, 0)

# ── Metrics Group Header Label ──────────────────────────────────────────────
$lblMH           = New-Object System.Windows.Forms.Label
$lblMH.Text      = "SYSTEM METRICS"
$lblMH.Font      = New-Object System.Drawing.Font("Segoe UI", 8, [System.Drawing.FontStyle]::Bold)
$lblMH.AutoSize  = $false
$lblMH.Dock      = "Fill"
$lblMH.Height    = 20
$lblMH.ForeColor = [System.Drawing.Color]::FromArgb(160,160,160)
$layout.Controls.Add($lblMH, 0, 1)

# ── Visual Metrics Grid Cards (Arranged horizontally via FlowLayoutPanel) ────
$metricPanel             = New-Object System.Windows.Forms.FlowLayoutPanel
$metricPanel.Dock        = "Fill"
$metricPanel.AutoSize    = $true
$metricPanel.BackColor   = [System.Drawing.Color]::FromArgb(40, 40, 40)
$metricPanel.Padding     = New-Object System.Windows.Forms.Padding(8)
$metricPanel.FlowDirection = "LeftToRight"

foreach ($item in @(
    @("CPU",    "$($d.cpu)%",                                      $d.cpu),
    @("RAM",    "$($d.ram_used)GB / $($d.ram_total)GB ($($d.ram)%)", $d.ram),
    @("Disk C", "$($d.disk_used)GB / $($d.disk_total)GB ($($d.disk)%)", $d.disk)
)) {{
    $box              = New-Object System.Windows.Forms.Panel
    $box.Size         = New-Object System.Drawing.Size(166, 60)
    $box.BackColor    = [System.Drawing.Color]::FromArgb(50, 50, 50)
    $box.Margin       = New-Object System.Windows.Forms.Padding(4)

    $lName            = New-Object System.Windows.Forms.Label
    $lName.Text       = $item[0]
    $lName.Font       = New-Object System.Drawing.Font("Segoe UI", 8)
    $lName.ForeColor  = [System.Drawing.Color]::FromArgb(180,180,180)
    $lName.Location   = New-Object System.Drawing.Point(8, 6)
    $lName.AutoSize   = $true

    $lVal             = New-Object System.Windows.Forms.Label
    $lVal.Text        = $item[1]
    $lVal.Font        = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
    $lVal.ForeColor   = ColorLabel $item[2] # Color assignment based on value
    $lVal.Location    = New-Object System.Drawing.Point(8, 26)
    $lVal.AutoSize    = $true

    $box.Controls.AddRange(@($lName, $lVal))
    $metricPanel.Controls.Add($box)
}}
$layout.Controls.Add($metricPanel, 0, 2)

# ── OS Event Terminal Panel ──────────────────────────────────────────────────
$evColor = if ($d.event_count -gt 0) {{ [System.Drawing.Color]::FromArgb(255,100,100) }} `
           else {{ [System.Drawing.Color]::FromArgb(80,200,80) }}
$evIcon  = if ($d.event_count -gt 0) {{ "WARNING" }} else {{ "OK" }}

$lblEv           = New-Object System.Windows.Forms.Label
$lblEv.Text      = "WINDOWS EVENTS   [$evIcon]  $($d.event_status)`n$($d.event_lines)"
$lblEv.Font      = New-Object System.Drawing.Font("Segoe UI", 8)
$lblEv.ForeColor = $evColor
$lblEv.AutoSize  = $false
$lblEv.Dock      = "Fill"
$lblEv.Height    = 90
$lblEv.Padding   = New-Object System.Windows.Forms.Padding(6)
$lblEv.BackColor = [System.Drawing.Color]::FromArgb(40, 40, 40)
$layout.Controls.Add($lblEv, 0, 3)

# ── Scrollable AI Interpretation Container ──────────────────────────────────
$lblAIH           = New-Object System.Windows.Forms.Label
$lblAIH.Text      = "GEMMA AI ANALYSIS"
$lblAIH.Font      = New-Object System.Drawing.Font("Segoe UI", 8, [System.Drawing.FontStyle]::Bold)
$lblAIH.ForeColor = [System.Drawing.Color]::FromArgb(160,160,160)
$lblAIH.Dock      = "Top"
$lblAIH.Height    = 18

$txtAI            = New-Object System.Windows.Forms.RichTextBox
$txtAI.Text       = $d.ai_response
$txtAI.Font       = New-Object System.Drawing.Font("Segoe UI", 10)
$txtAI.BackColor  = [System.Drawing.Color]::FromArgb(40, 40, 40)
$txtAI.ForeColor  = [System.Drawing.Color]::White
$txtAI.ReadOnly   = $true
$txtAI.Dock       = "Fill"
$txtAI.BorderStyle = "None"
$txtAI.WordWrap   = $true
$txtAI.ScrollBars = "Vertical"
$txtAI.Padding    = New-Object System.Windows.Forms.Padding(6)

$aiPanel          = New-Object System.Windows.Forms.Panel
$aiPanel.Dock     = "Fill"
$aiPanel.BackColor = [System.Drawing.Color]::FromArgb(40, 40, 40)
$aiPanel.Padding  = New-Object System.Windows.Forms.Padding(4)
$aiPanel.Controls.Add($txtAI)
$aiPanel.Controls.Add($lblAIH)
$layout.Controls.Add($aiPanel, 0, 4)

# ── Window Action Dismiss Button ──────────────────────────────────────────────
$btnClose              = New-Object System.Windows.Forms.Button
$btnClose.Text         = "Close"
$btnClose.Size         = New-Object System.Drawing.Size(120, 34)
$btnClose.Anchor       = "Bottom,Right"
$btnClose.BackColor    = [System.Drawing.Color]::FromArgb(60, 60, 60)
$btnClose.ForeColor    = [System.Drawing.Color]::White
$btnClose.FlatStyle    = "Flat"
$btnClose.Dock         = "Right"
$btnClose.Add_Click({{ $form.Close() }})

$btnPanel              = New-Object System.Windows.Forms.Panel
$btnPanel.Height       = 48
$btnPanel.Dock         = "Fill"
$btnPanel.BackColor    = [System.Drawing.Color]::FromArgb(30, 30, 30)
$btnPanel.Controls.Add($btnClose)
$layout.Controls.Add($btnPanel, 0, 5)

# Automatic lifecycle garbage cleanup closure rule after 20 seconds
$timer          = New-Object System.Windows.Forms.Timer
$timer.Interval = 20000
$timer.Add_Tick({{ $form.Close(); $timer.Stop() }})
$timer.Start()

[void]$form.ShowDialog()
"""
        # Execute the generated runtime configuration inside a detached non-blocking subprocess pipeline
        subprocess.Popen(
            ['powershell', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
             '-Command', ps_script],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
    except Exception as e:
        log(f"[POPUP] Error: {e}")

# ── Manual prompt thread ──────────────────────────────────────────────────────
def manual_prompt_thread():
    """Runs a dedicated shell interface thread for standalone, custom user questions down to Gemma."""
    print("\n  Type any question for Gemma at any time.", flush=True)
    print("  Type 'quit' to stop manual mode (monitor keeps running).\n", flush=True)
    while True:
        try:
            user_input = input(">>> Ask Gemma: ")
        except EOFError:
            break
        if user_input.lower() == 'quit':
            print("  Manual mode stopped. Auto monitor continues.", flush=True)
            break
        if user_input.strip():
            log(f"[MANUAL] You asked: {user_input}")
            try:
                answer = ask_ai_custom(user_input)
                log(f"[MANUAL] Gemma says: {answer}")
            except Exception as e:
                log(f"[MANUAL] ERROR: {e}")

# ── Main Loop Runtime Execution ───────────────────────────────────────────────
def main():
    global cycle_count

    log_startup()
    # Spin up the concurrent manual terminal prompt pipeline as a background Daemon thread
    threading.Thread(target=manual_prompt_thread, daemon=True).start()

    while True:
        try:
            cycle_count += 1
            print(f"\n>> [Cycle {cycle_count}] Collecting system metrics...", flush=True)
            stats = get_system_stats()

            print(f">> [Cycle {cycle_count}] Reading Windows Event Log...", flush=True)
            events           = get_windows_events()
            real_event_count = len([e for e in events if "fetch_error" not in e])

            print(f">> [Cycle {cycle_count}] Asking Gemma (events: {real_event_count})...", flush=True)
            ai_response = ask_ai(stats, events)

            # Record system resource allocations and linguistic analysis blocks to local text file
            log(f"[AUTO] CPU={stats['cpu_percent']}%  "
                f"RAM={stats['ram_percent']}%  "
                f"DISK={stats['disk_percent']}%  "
                f"WinEvents={real_event_count}")
            log(f"[AUTO] Gemma: {ai_response}")

            # Periodically deliver visual UI panels to user based on modular tracking cycles
            if cycle_count % POPUP_EVERY_N == 0:
                show_popup(stats, events, ai_response)
                log("[POPUP] Health summary popup displayed")

        except Exception as e:
            log(f"[AUTO] ERROR: {e}")

        # Sleep thread execution until the next cycle timestamp boundary
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()