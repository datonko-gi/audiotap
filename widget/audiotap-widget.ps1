#requires -version 5
# AudioTap widget: record/stop, recordings folder, device + mic footer. 168x56 physical px.
# It is only the front panel: the recorder is ..\app\audiotap.py, and the supervisor
# (..\app\supervisor.py) keeps this widget alive.
#
# Chrome recipe:
#  - SetProcessDPIAware BEFORE any window: without it DWM stretches the window 1.25x
#    with bilinear smearing, which shows up as a wrong tint and stepped corners
#  - rounded corners from a window region, so there is no DWM shadow
#  - buttons: vertical gradient face + 1px semi-transparent border pen

$ErrorActionPreference = 'SilentlyContinue'

# DPI awareness first, before ANY assembly that touches GDI/DWM
Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class DpiPre {[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();}'
[DpiPre]::SetProcessDPIAware() | Out-Null

# ---------------------------------------------------------------- paths
# This file lives in <repo>\widget, so everything is found relative to it.
$ROOT       = Split-Path -Parent $PSScriptRoot
$APP        = Join-Path $ROOT 'app\audiotap.py'
$SUPERVISOR = Join-Path $ROOT 'app\supervisor.py'
$WORK       = Join-Path $env:LOCALAPPDATA 'AudioTap'      # working files, shared with the Python app
$STATUS     = Join-Path $WORK 'status.json'
$LAST       = Join-Path $WORK 'last.json'
$CONFIG     = Join-Path $WORK 'config.json'
$LOG        = Join-Path $WORK 'widget-runs.log'
$WLOG       = Join-Path $WORK 'widget.log'
$PINFILE    = Join-Path $WORK 'python_exe.txt'
$DATADIR    = Join-Path $env:APPDATA 'AudioTapWidget'     # widget position and options
$DATA       = Join-Path $DATADIR 'data.json'
$HARDCAP    = 14400
if (-not (Test-Path -LiteralPath $WORK)) { New-Item -ItemType Directory -Path $WORK | Out-Null }

function Write-WLog($m) {
    try { Add-Content -LiteralPath $WLOG -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8 } catch { }
}

# Interpreter resolution: the pin the supervisor writes at every start (python_exe.txt,
# the pythonw.exe that has the packages) -> python.exe on PATH -> the py launcher pinned
# to Python 3. The pin goes first because a Microsoft Store "Python Install Manager" shim
# can sit first on PATH and delegate to an interpreter without numpy or PyAudioWPatch.
# It is resolved again before every launch, because the supervisor writes the pin a moment
# after the first start.
function Resolve-Interpreter {
    try {
        if (Test-Path -LiteralPath $PINFILE) {
            $p = (Get-Content -LiteralPath $PINFILE -Raw -ErrorAction Stop).Trim()
            if ($p -and (Test-Path -LiteralPath $p)) {
                $py = $p -replace 'pythonw\.exe$', 'python.exe'
                if (-not (Test-Path -LiteralPath $py)) { $py = $p }
                return @{ PY = $py; PYW = $p; PreArgs = ''; Method = 'python_exe.txt' }
            }
        }
    } catch { }
    try {
        $cmd = Get-Command python.exe -ErrorAction Stop
        $py = $cmd.Source
        $pyw = $py -replace 'python\.exe$', 'pythonw.exe'
        if (-not (Test-Path -LiteralPath $pyw)) { $pyw = $py }
        return @{ PY = $py; PYW = $pyw; PreArgs = ''; Method = 'PATH' }
    } catch { }
    try {
        $cmd = Get-Command py.exe -ErrorAction Stop
        $pyw = Join-Path (Split-Path -Parent $cmd.Source) 'pyw.exe'
        if (-not (Test-Path -LiteralPath $pyw)) { $pyw = $cmd.Source }
        return @{ PY = $cmd.Source; PYW = $pyw; PreArgs = '-3'; Method = 'py launcher -3' }
    } catch { }
    return @{ PY = 'python.exe'; PYW = 'pythonw.exe'; PreArgs = ''; Method = 'fallback literal' }
}

function Update-Interpreter {
    $script:Interp = Resolve-Interpreter
    $script:PY = $script:Interp.PY
    $script:PYW = $script:Interp.PYW
}
Update-Interpreter
Write-WLog "interpreter resolved via $($Interp.Method): PY=$PY PYW=$PYW"

# ---------------------------------------------------------------- single instance
# Pattern built by concatenation: a literal would match OTHER processes carrying the
# search string in their command line (the supervisor's liveness probe).
$SELFMATCH = '*audiotap-wid' + 'get.ps1*'
$mutex = New-Object System.Threading.Mutex($false, 'Global\AudioTapWidget')
if (-not $mutex.WaitOne(0)) { exit }
[System.Runtime.InteropServices.GCHandle]::Alloc($mutex) | Out-Null
$others = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
          Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like $SELFMATCH }
if ($others) { exit }

# this is a deliberate launch (Startup, install, or a hand-run), so clear any manual-close
# flag: from now on the supervisor may keep the widget alive again
$WIDGET_CLOSED = Join-Path $WORK 'widget_closed.flag'
Remove-Item -LiteralPath $WIDGET_CLOSED -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- state
$default = @{ version = 7; posX = -99999; posY = -99999; mic = $true; device = '' }
$state = $default.Clone()
if (Test-Path -LiteralPath $DATA) {
    try {
        $loaded = Get-Content -LiteralPath $DATA -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($k in @($default.Keys)) { if ($null -ne $loaded.$k) { $state[$k] = $loaded.$k } }
    } catch { }
}
# a saved position counts only while it lies on a connected screen (negative coordinates are
# valid on a monitor left of or above the primary; an unplugged monitor must not hide the widget)
function Test-OnScreen($x, $y) {
    if ($null -eq $x -or $null -eq $y -or [int]$x -le -99999) { return $false }
    foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
        if ($s.WorkingArea.Contains(([int]$x + 20), ([int]$y + 20))) { return $true }
    }
    return $false
}

function Save-State {
    if (-not (Test-Path -LiteralPath $DATADIR)) { New-Item -ItemType Directory -Path $DATADIR | Out-Null }
    ($state | ConvertTo-Json -Compress) | Set-Content -LiteralPath $DATA -Encoding UTF8
}

# ---------------------------------------------------------------- native
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class NativeW {
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int idx);
    [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int idx, int v);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("dwmapi.dll")] public static extern int DwmSetWindowAttribute(IntPtr h, int attr, ref int val, int size);
    [DllImport("gdi32.dll")] public static extern IntPtr CreateRoundRectRgn(int x1, int y1, int x2, int y2, int cw, int ch);
    [DllImport("user32.dll")] public static extern int SetWindowRgn(IntPtr h, IntPtr rgn, bool redraw);
    [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vk);
    [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint cmd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder sb, int max);
    public static readonly IntPtr HWND_BOTTOM = new IntPtr(1);
    // Re-sink ONLY when actually raised: unconditional HWND_BOTTOM makes DWM redo the
    // drop shadow every tick, which blinks. Bottom = first visible window below us is
    // the shell or another sunk widget.
    public static void SinkIfRaised(IntPtr hwnd) {
        IntPtr below = GetWindow(hwnd, 2); // GW_HWNDNEXT
        int guard = 0;
        while (below != IntPtr.Zero && !IsWindowVisible(below) && guard++ < 32) below = GetWindow(below, 2);
        string cls = "";
        if (below != IntPtr.Zero) { var sb = new System.Text.StringBuilder(64); GetClassName(below, sb, 64); cls = sb.ToString(); }
        if (below == IntPtr.Zero || cls == "Progman" || cls == "WorkerW" || cls.StartsWith("WindowsForms10")) return;
        SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, 0x0013);
    }
}
"@

# Low-level global mouse hook: a NOACTIVATE tool window's ContextMenuStrip does not get
# the outside-click that normally dismisses it, so the menu lingered. This hook fires on
# the actual button-down event (no 150 ms poll) and hands the screen point to a managed
# callback, which closes the menu instantly when the click lands outside it.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class MouseHook {
    public delegate void DownHandler(int x, int y);
    public static DownHandler OnDown;
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate IntPtr LowLevelProc(int nCode, IntPtr wParam, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential)] struct POINT { public int x; public int y; }
    [StructLayout(LayoutKind.Sequential)] struct MSLLHOOKSTRUCT { public POINT pt; public uint mouseData; public uint flags; public uint time; public IntPtr dwExtraInfo; }
    [DllImport("user32.dll")] static extern IntPtr SetWindowsHookEx(int idHook, LowLevelProc lpfn, IntPtr hMod, uint dwThreadId);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hhk);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);
    [DllImport("kernel32.dll", CharSet = CharSet.Auto)] static extern IntPtr GetModuleHandle(string name);
    static IntPtr _hook = IntPtr.Zero;
    static LowLevelProc _proc = Proc;              // kept alive for the process lifetime
    const int WH_MOUSE_LL = 14;
    static IntPtr Proc(int nCode, IntPtr wParam, IntPtr lParam) {
        if (nCode >= 0) {
            int m = (int)wParam;
            if (m == 0x0201 || m == 0x0204 || m == 0x0207) {   // L / R / M button down
                var s = (MSLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(MSLLHOOKSTRUCT));
                var h = OnDown;
                if (h != null) { try { h(s.pt.x, s.pt.y); } catch {} }
            }
        }
        return CallNextHookEx(_hook, nCode, wParam, lParam);
    }
    public static void Install() {
        if (_hook == IntPtr.Zero)
            _hook = SetWindowsHookEx(WH_MOUSE_LL, _proc, GetModuleHandle(null), 0);
    }
    public static void Uninstall() {
        if (_hook != IntPtr.Zero) { UnhookWindowsHookEx(_hook); _hook = IntPtr.Zero; }
    }
}
"@

# ---------------------------------------------------------------- helpers
function Read-Json($path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

# recordings_dir from config.json, else a "recordings" folder next to the app
function Get-RecordsDir {
    $cfg = Read-Json $CONFIG
    if ($cfg -and $cfg.recordings_dir -and ([string]$cfg.recordings_dir).Trim()) {
        return [Environment]::ExpandEnvironmentVariables(([string]$cfg.recordings_dir).Trim())
    }
    return (Join-Path $ROOT 'recordings')
}

function Get-RecorderProcess {
    return (Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
            Where-Object { $_.CommandLine -like '*audiotap.py*' -and $_.CommandLine -notlike '*--devices-json*' -and $_.CommandLine -notlike '*--list*' } |
            Select-Object -First 1)
}

function Start-Recording {
    Update-Interpreter
    $stopFile = Join-Path (Get-RecordsDir) 'STOP'
    if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile -Force }
    $a = @("`"$APP`"")
    if ($state.mic) { $a += '--mic' }
    if ($state.device) { $a += @('--device', "`"$($state.device)`"") }
    # --transcribe: the recording postprocesses itself when it closes, so a transcript
    # does not depend on the supervisor sweep being alive at that moment
    $a += @('--quiet', '--transcribe', '--seconds', "$HARDCAP", '--log', "`"$LOG`"")
    if ($Interp.PreArgs) { $a = @($Interp.PreArgs) + $a }
    Start-Process -FilePath $PYW -ArgumentList $a -WindowStyle Hidden
}

function Stop-Recording {
    $st = Read-Json $STATUS
    $sf = Join-Path (Get-RecordsDir) 'STOP'
    if ($st -and $st.stop_file) { $sf = $st.stop_file }
    'stop' | Set-Content -LiteralPath $sf -Encoding ASCII
}

function Open-Records {
    $last = Read-Json $LAST
    if ($last -and $last.open_path -and (Test-Path -LiteralPath $last.open_path)) {
        Start-Process explorer.exe "/select,`"$($last.open_path)`""
    } else {
        $dir = Get-RecordsDir
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
        Start-Process explorer.exe "`"$dir`""
    }
}

function Get-Taps {
    Update-Interpreter
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $PY
        $preA = if ($Interp.PreArgs) { $Interp.PreArgs + ' ' } else { '' }
        $psi.Arguments = $preA + '"' + $APP + '" --devices-json'
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $raw = $p.StandardOutput.ReadToEnd()
        $p.WaitForExit(15000) | Out-Null
        if ($raw) { return ($raw | ConvertFrom-Json) }
    } catch { }
    return $null
}

function Format-Clock([int]$sec) {
    if ($sec -ge 3600) {
        return ('{0}:{1:00}:{2:00}' -f [math]::Floor($sec / 3600), [math]::Floor(($sec % 3600) / 60), ($sec % 60))
    }
    return ('{0:00}:{1:00}' -f [math]::Floor($sec / 60), ($sec % 60))
}


function New-RectF($r) {
    return (New-Object System.Drawing.RectangleF([single]$r.X, [single]$r.Y, [single]$r.Width, [single]$r.Height))
}

# ---------------------------------------------------------------- look: 168x56 PHYSICAL px
$W = 168; $H = 56
$C = @{
    bg      = [System.Drawing.Color]::FromArgb(248, 248, 248)
    text    = [System.Drawing.Color]::FromArgb(70, 70, 78)
    faint   = [System.Drawing.Color]::FromArgb(150, 150, 158)
    btnHot  = [System.Drawing.Color]::FromArgb(236, 236, 240)
    edge    = [System.Drawing.Color]::FromArgb(80, 0, 0, 0)      # button border pen
    edgeHot = [System.Drawing.Color]::FromArgb(120, 0, 0, 0)     # pressed / active border pen
    gradTop = [System.Drawing.Color]::FromArgb(250, 250, 252)    # button face
    gradBot = [System.Drawing.Color]::FromArgb(215, 215, 222)
    red     = [System.Drawing.Color]::FromArgb(206, 68, 54)
    amber   = [System.Drawing.Color]::FromArgb(224, 162, 54)
    amberIn = [System.Drawing.Color]::FromArgb(110, 76, 0)
    orange  = [System.Drawing.Color]::FromArgb(226, 119, 40)
    green   = [System.Drawing.Color]::FromArgb(58, 158, 102)
    greenHi = [System.Drawing.Color]::FromArgb(84, 205, 134)
    off     = [System.Drawing.Color]::FromArgb(186, 186, 192)
    white   = [System.Drawing.Color]::White
    strip   = [System.Drawing.Color]::FromArgb(236, 188, 182)
}
$F = @{
    btn   = New-Object System.Drawing.Font('Segoe UI', 8, [System.Drawing.FontStyle]::Bold)
    foot  = New-Object System.Drawing.Font('Segoe UI', 6.5)
}
$RECT = @{
    main   = New-Object System.Drawing.Rectangle(5, 5, 105, 32)
    folder = New-Object System.Drawing.Rectangle(115, 5, 48, 32)
}

function New-RoundPath($r, [single]$rad) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $rad * 2.0
    $p.AddArc([single]$r.X, [single]$r.Y, $d, $d, 180, 90)
    $p.AddArc([single]($r.Right - $d), [single]$r.Y, $d, $d, 270, 90)
    $p.AddArc([single]($r.Right - $d), [single]($r.Bottom - $d), $d, $d, 0, 90)
    $p.AddArc([single]$r.X, [single]($r.Bottom - $d), $d, $d, 90, 90)
    $p.CloseFigure()
    return $p
}

# ---------------------------------------------------------------- live values
$script:mode      = 'idle'      # idle | rec | mixing | dead
$script:external  = $false
$script:elapsed   = 0
$script:sysDb     = -99.0
$script:micDb     = -99.0
$script:sysShown  = -99.0
$script:devLabel  = ''
$script:hover     = ''
$script:lastSig   = ''
$script:tick      = 0
$script:extStart  = $null
$script:loudAt    = $null
$script:silentFor = 0

function Step-Meter([double]$shown, [double]$now) {
    if ($now -gt $shown) { return $now }
    return [math]::Max($now, $shown - 10.0)
}

# ---------------------------------------------------------------- form
$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = 'None'
$form.ShowInTaskbar = $false
$form.TopMost = $false
$form.StartPosition = 'Manual'
$form.Size = New-Object System.Drawing.Size($W, $H)
$form.BackColor = $C.bg
$form.GetType().GetProperty('DoubleBuffered', [System.Reflection.BindingFlags]'Instance,NonPublic').SetValue($form, $true, $null)
if (Test-OnScreen $state.posX $state.posY) {
    $form.Location = New-Object System.Drawing.Point([int]$state.posX, [int]$state.posY)
} else {
    $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $form.Location = New-Object System.Drawing.Point(($wa.Right - $W - 46), ($wa.Bottom - $H - 10))
}

# ---------------------------------------------------------------- drawing
function Draw-Widget($g) {
    $g.SmoothingMode = 'AntiAlias'
    # grayscale AA: ClearType subpixel fringes smear into a gray-blue haze when the
    # window is DPI-stretched
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit

    $sfc = New-Object System.Drawing.StringFormat
    $sfc.Alignment = 'Center'
    $sfc.LineAlignment = 'Center'

    # ---- main button
    $mp = New-RoundPath $RECT.main 7.0
    $cy = $RECT.main.Y + ($RECT.main.Height / 2)
    if ($script:mode -eq 'rec') {
        if ($script:silentFor -ge 30) { $fill = $C.amber } else { $fill = $C.red }
        $g.FillPath((New-Object System.Drawing.SolidBrush($fill)), $mp)
        $g.DrawPath((New-Object System.Drawing.Pen($C.edgeHot, 1.0)), $mp)
        $sq = New-Object System.Drawing.Rectangle(($RECT.main.X + 12), ([int]$cy - 5), 10, 10)
        $sp = New-RoundPath $sq 2.5
        $g.FillPath((New-Object System.Drawing.SolidBrush($C.white)), $sp)
        $sp.Dispose()
        $g.DrawString((Format-Clock $script:elapsed), $F.btn, (New-Object System.Drawing.SolidBrush($C.white)), ($RECT.main.X + 28), ($cy - 10))
        # thin OUT-level strip: live proof audio is flowing
        $frac = [math]::Max(0.0, [math]::Min(1.0, ($script:sysDb + 60.0) / 60.0))
        $g.FillRectangle((New-Object System.Drawing.SolidBrush($C.strip)), ($RECT.main.X + 8), ($RECT.main.Bottom - 6), ($RECT.main.Width - 16), 3)
        if ($frac -gt 0.02) {
            $g.FillRectangle((New-Object System.Drawing.SolidBrush($C.white)), ($RECT.main.X + 8), ($RECT.main.Bottom - 6), [int](($RECT.main.Width - 16) * $frac), 3)
        }
    }
    elseif ($script:mode -eq 'mixing') {
        $g.FillPath((New-Object System.Drawing.SolidBrush($C.amber)), $mp)
        $g.DrawPath((New-Object System.Drawing.Pen($C.edgeHot, 1.0)), $mp)
        $g.DrawString('MIXING', $F.btn, (New-Object System.Drawing.SolidBrush($C.amberIn)), (New-RectF $RECT.main), $sfc)
    }
    elseif ($script:mode -eq 'dead') {
        $g.FillPath((New-Object System.Drawing.SolidBrush($C.orange)), $mp)
        $g.DrawPath((New-Object System.Drawing.Pen($C.edgeHot, 1.0)), $mp)
        $g.DrawString('STOPPED', $F.btn, (New-Object System.Drawing.SolidBrush($C.white)), (New-RectF $RECT.main), $sfc)
    }
    else {
        # button chrome: vertical gradient face + semi-transparent 1px border
        if ($script:hover -eq 'main') {
            $g.FillPath((New-Object System.Drawing.SolidBrush($C.btnHot)), $mp)
        } else {
            $gb = New-Object System.Drawing.Drawing2D.LinearGradientBrush((New-RectF $RECT.main), $C.gradTop, $C.gradBot, [System.Drawing.Drawing2D.LinearGradientMode]::Vertical)
            $g.FillPath($gb, $mp)
            $gb.Dispose()
        }
        $g.DrawPath((New-Object System.Drawing.Pen($C.edge, 1.0)), $mp)
        $g.FillEllipse((New-Object System.Drawing.SolidBrush($C.red)), ($RECT.main.X + 11), ([int]$cy - 6), 12, 12)
        $g.DrawString('RECORD', $F.btn, (New-Object System.Drawing.SolidBrush($C.text)), ($RECT.main.X + 30), ($cy - 10))
    }
    $mp.Dispose()

    # ---- folder button, same chrome
    $fp = New-RoundPath $RECT.folder 7.0
    if ($script:hover -eq 'folder') {
        $g.FillPath((New-Object System.Drawing.SolidBrush($C.btnHot)), $fp)
    } else {
        $gb2 = New-Object System.Drawing.Drawing2D.LinearGradientBrush((New-RectF $RECT.folder), $C.gradTop, $C.gradBot, [System.Drawing.Drawing2D.LinearGradientMode]::Vertical)
        $g.FillPath($gb2, $fp)
        $gb2.Dispose()
    }
    $g.DrawPath((New-Object System.Drawing.Pen($C.edge, 1.0)), $fp)
    $fp.Dispose()
    $fx = $RECT.folder.X + 9
    $fy = $RECT.folder.Y + 12
    $brF = New-Object System.Drawing.SolidBrush($C.faint)
    $tab = New-Object System.Drawing.Rectangle($fx, ($fy - 4), 12, 4)
    $body = New-Object System.Drawing.Rectangle($fx, $fy, 30, 15)
    $tp = New-RoundPath $tab 1.5
    $bp = New-RoundPath $body 2.5
    $g.FillPath($brF, $tp)
    $g.FillPath($brF, $bp)
    $tp.Dispose(); $bp.Dispose()

    # ---- footer: output device on the left, microphone dot on the right
    $dev = $script:devLabel
    if (-not $dev) { if ($state.device) { $dev = $state.device } else { $dev = 'all outputs' } }
    while ([int]$g.MeasureString($dev, $F.foot).Width -gt 136 -and $dev.Length -gt 6) {
        $dev = $dev.Substring(0, $dev.Length - 4) + '..'
    }
    $g.DrawString($dev, $F.foot, (New-Object System.Drawing.SolidBrush($C.faint)), 8, 41)

    if ($state.mic) {
        if ($script:mode -eq 'rec' -and $script:micDb -gt -50) { $mcol = $C.greenHi } else { $mcol = $C.green }
    } else { $mcol = $C.off }
    $g.FillEllipse((New-Object System.Drawing.SolidBrush($mcol)), 151, 43, 9, 9)
}

function Render-Widget {
    $form.Invalidate()
}

$form.Add_Paint({
    param($sender, $e)
    Draw-Widget $e.Graphics
})

# ---------------------------------------------------------------- mouse
$script:dragging = $false
$script:downPt = New-Object System.Drawing.Point(0, 0)
$script:formPt = New-Object System.Drawing.Point(0, 0)

function Hit-Test([int]$x, [int]$y) {
    if ($RECT.main.Contains($x, $y))   { return 'main' }
    if ($RECT.folder.Contains($x, $y)) { return 'folder' }
    return ''
}

$form.Add_MouseDown({
    param($sender, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        $script:dragging = $false
        $script:downPt = [System.Windows.Forms.Cursor]::Position
        $script:formPt = $form.Location
    }
})

$form.Add_MouseMove({
    param($sender, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        $now = [System.Windows.Forms.Cursor]::Position
        $dx = $now.X - $script:downPt.X
        $dy = $now.Y - $script:downPt.Y
        if (-not $script:dragging -and ([math]::Abs($dx) + [math]::Abs($dy)) -gt 5) { $script:dragging = $true }
        if ($script:dragging) {
            $form.Location = New-Object System.Drawing.Point(($script:formPt.X + $dx), ($script:formPt.Y + $dy))
        }
    } else {
        $ht = Hit-Test $e.X $e.Y
        if ($ht -ne $script:hover) { $script:hover = $ht; Render-Widget }
    }
})

$form.Add_MouseLeave({
    param($sender, $e)
    if ($script:hover) { $script:hover = ''; Render-Widget }
})

$form.Add_MouseUp({
    param($sender, $e)
    if ($e.Button -ne [System.Windows.Forms.MouseButtons]::Left) { return }
    if ($script:dragging) {
        $script:dragging = $false
        $state.posX = $form.Location.X
        $state.posY = $form.Location.Y
        Save-State
        return
    }
    $hit = Hit-Test $e.X $e.Y
    if ($hit -eq 'main') {
        if ($script:mode -eq 'rec') { Stop-Recording }
        elseif ($script:mode -ne 'mixing') { Start-Recording }
        $script:lastSig = ''
    }
    elseif ($hit -eq 'folder') { Open-Records }
})

# ---------------------------------------------------------------- right click menu
# A NOACTIVATE owner window never takes focus, so its popup menus miss the usual
# focus-loss dismissal. The low-level mouse hook fires on the real button-down and closes
# any tracked menu instantly when the click lands outside it (no poll, no lag).
$script:openMenus = New-Object System.Collections.ArrayList

function Close-MenusIfOutside([int]$px, [int]$py) {
    $pt = New-Object System.Drawing.Point($px, $py)
    $anyVisible = $false
    $inside = $false
    foreach ($m in @($script:openMenus)) {
        if ($m.Visible) {
            $anyVisible = $true
            if ($m.Bounds.Contains($pt)) { $inside = $true }
            foreach ($it in $m.Items) {
                if ($it -is [System.Windows.Forms.ToolStripMenuItem] -and $it.DropDown.Visible -and
                    $it.DropDown.Bounds.Contains($pt)) { $inside = $true }
            }
        }
    }
    if ($anyVisible -and -not $inside) {
        foreach ($m in @($script:openMenus)) { if ($m.Visible) { $m.Close() } }
    }
}
# hook callback runs on the UI thread (hook installed there), so touching menus is safe
[MouseHook]::OnDown = [MouseHook+DownHandler]{ param($x, $y) Close-MenusIfOutside $x $y }

$ctx = New-Object System.Windows.Forms.ContextMenuStrip
[void]$script:openMenus.Add($ctx)
$miDevice = $ctx.Items.Add('Output device')
$miDevice.Add_Click({
    param($s, $ev)
    $menu = New-Object System.Windows.Forms.ContextMenuStrip
    [void]$script:openMenus.Add($menu)
    $menu.Add_Closed({ param($s, $ev) $script:openMenus.Remove($s) })
    $auto = $menu.Items.Add('All output devices')
    $auto.Checked = (-not $state.device)
    $auto.Add_Click({ param($s2, $ev2) $state.device = ''; Save-State; Render-Widget })
    $taps = Get-Taps
    if ($taps) {
        foreach ($t in $taps.taps) {
            $it = $menu.Items.Add([string]$t.name)
            $it.Checked = ($state.device -eq $t.name)
            $it.Tag = [string]$t.name
            $it.Add_Click({ param($s2, $ev2) $state.device = [string]$s2.Tag; Save-State; Render-Widget })
        }
    } else {
        $err = $menu.Items.Add('(device list unavailable)')
        $err.Enabled = $false
    }
    $menu.Show([System.Windows.Forms.Cursor]::Position)
})
$miMic = $ctx.Items.Add('Record microphone')
$miMic.Checked = [bool]$state.mic
$miMic.Add_Click({
    param($s, $ev)
    $state.mic = -not $state.mic
    $s.Checked = $state.mic
    Save-State
    $script:lastSig = ''
    Render-Widget
})
$ctx.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
$miFolder = $ctx.Items.Add('Open recordings folder')
$miFolder.Add_Click({ param($s, $ev) Open-Records })
$miClose = $ctx.Items.Add('Close widget')
$miClose.Add_Click({
    param($s, $ev)
    # deliberate close: tell the supervisor to leave it closed (until a fresh launch)
    'closed' | Set-Content -LiteralPath $WIDGET_CLOSED -Encoding ASCII
    $form.Close()
})
$form.ContextMenuStrip = $ctx

# ---------------------------------------------------------------- z-order + corners
$form.Add_Shown({
    param($sender, $e)
    $h2 = $form.Handle
    $ex = [NativeW]::GetWindowLong($h2, -20)
    [NativeW]::SetWindowLong($h2, -20, ($ex -bor 0x08000000 -bor 0x00000080)) | Out-Null
    # Region rounding, NOT the DWM attribute: a region has no shadow by construction
    $rgn = [NativeW]::CreateRoundRectRgn(0, 0, ($W + 1), ($H + 1), 14, 14)
    [NativeW]::SetWindowRgn($h2, $rgn, $true) | Out-Null
    [NativeW]::SetWindowPos($h2, [NativeW]::HWND_BOTTOM, 0, 0, 0, 0, (0x0002 -bor 0x0001 -bor 0x0010)) | Out-Null
    [MouseHook]::Install()   # on the UI thread, so the outside-click callback is thread-safe
})

$script:sinkCount = 0
$sink = New-Object System.Windows.Forms.Timer
$sink.Interval = 10000
$sink.Add_Tick({
    param($sender, $e)
    [NativeW]::SinkIfRaised($form.Handle)
    $lower = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
             Where-Object { $_.ProcessId -lt $PID -and $_.CommandLine -like $SELFMATCH }
    if ($lower) { $form.Close() }

    # reverse watchdog: bring the supervisor back if its heartbeat goes stale
    $script:sinkCount++
    if (($script:sinkCount % 9) -eq 0) {
        $hb = Join-Path $WORK 'supervisor_heartbeat.txt'
        $stale = $true
        $hbItem = Get-Item -LiteralPath $hb
        if ($hbItem) {
            $stale = ((Get-Date).ToUniversalTime() - $hbItem.LastWriteTimeUtc).TotalSeconds -gt 120
        }
        if ($stale) {
            if (Test-Path -LiteralPath $SUPERVISOR) {
                # log every rescue, so a watchdog that keeps firing against a wedged
                # supervisor leaves a trace
                $hbAge = -1
                if ($hbItem) { $hbAge = [int]((Get-Date).ToUniversalTime() - $hbItem.LastWriteTimeUtc).TotalSeconds }
                try {
                    $wline = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] widget watchdog: heartbeat ' + $hbAge + ' s stale, starting supervisor' + [Environment]::NewLine
                    [System.IO.File]::AppendAllText((Join-Path $WORK 'supervisor.log'), $wline, (New-Object System.Text.UTF8Encoding($false)))
                } catch { }
                Update-Interpreter
                $psi = New-Object System.Diagnostics.ProcessStartInfo
                $psi.FileName = $PYW
                $preA = if ($Interp.PreArgs) { $Interp.PreArgs + ' ' } else { '' }
                $psi.Arguments = $preA + '"' + $SUPERVISOR + '"'
                $psi.UseShellExecute = $false
                $psi.CreateNoWindow = $true
                [System.Diagnostics.Process]::Start($psi) | Out-Null
            }
        }
    }
})
$sink.Start()

# ---------------------------------------------------------------- poll
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 400
$timer.Add_Tick({
    param($sender, $e)
    $script:tick++
    $st = Read-Json $STATUS
    $stFile = Get-Item -LiteralPath $STATUS
    $wasRec = ($script:mode -eq 'rec')

    if ($st -and $st.state -eq 'recording') {
        $age = 0
        if ($stFile) { $age = ((Get-Date).ToUniversalTime() - $stFile.LastWriteTimeUtc).TotalSeconds }
        if ($age -gt 15) {
            $script:mode = 'dead'
        } else {
            $script:mode = 'rec'
            $script:external = $false
            $script:elapsed = [int]$st.elapsed
            $script:sysDb = Step-Meter $script:sysShown ([double]$st.sys_db)
            $script:sysShown = $script:sysDb
            if ($null -ne $st.mic_db) { $script:micDb = [double]$st.mic_db } else { $script:micDb = -99.0 }
            $script:devLabel = [string]$st.device
            if ([double]$st.sys_db -gt -60) { $script:loudAt = Get-Date }
            if (-not $script:loudAt) { $script:loudAt = Get-Date }
            $script:silentFor = ((Get-Date) - $script:loudAt).TotalSeconds
        }
    }
    elseif ($st -and $st.state -eq 'finishing') {
        $script:mode = 'mixing'
    }
    else {
        if (($script:tick % 5) -eq 0 -or $wasRec) {
            $rp = Get-RecorderProcess
            $script:external = [bool]$rp
            if ($rp) { $script:extStart = $rp.CreationDate }
        }
        if ($script:external) {
            $script:mode = 'rec'
            if ($script:extStart) { $script:elapsed = [int]((Get-Date) - $script:extStart).TotalSeconds }
            $script:sysDb = -99.0
            $script:micDb = -99.0
            $script:silentFor = 0
            $script:devLabel = ''
        } else {
            $script:mode = 'idle'
            $script:sysDb = -99.0
            $script:micDb = -99.0
            $script:sysShown = -99.0
            $script:silentFor = 0
            $script:devLabel = ''
            $script:loudAt = $null
        }
    }

    $sig = '{0}|{1}|{2}|{3}|{4}|{5}|{6}' -f $script:mode, $script:elapsed,
           [math]::Round($script:sysDb), ($script:micDb -gt -50), $state.mic,
           $script:devLabel, [int]($script:silentFor / 5)
    if ($sig -ne $script:lastSig) {
        $script:lastSig = $sig
        Render-Widget
    }
})
$timer.Start()

[void]$form.ShowDialog()
