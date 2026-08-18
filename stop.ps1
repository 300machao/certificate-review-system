$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $ProjectRoot "data\server.pid"

if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host "未发现本系统的运行标记；没有停止其他进程。"
    exit 0
}

$Marker = $null
try { $Marker = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json } catch {}
$RecordedPid = 0
$ServerPort = 0
if (-not $Marker -or -not [int]::TryParse([string]$Marker.pid, [ref]$RecordedPid) -or
    -not [int]::TryParse([string]$Marker.port, [ref]$ServerPort) -or
    [string]$Marker.project -ne $ProjectRoot) {
    Write-Host "运行标记无效；为安全起见没有停止任何进程。"
    exit 1
}

$LauncherPid = 0
if ($Marker.PSObject.Properties.Name -contains "launcher_pid") {
    if (-not [int]::TryParse([string]$Marker.launcher_pid, [ref]$LauncherPid)) {
        Write-Host "运行标记中的启动器 PID 无效；为安全起见没有停止任何进程。"
        exit 1
    }
}

$Listeners = @(
    Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $ServerPort -State Listen `
        -ErrorAction SilentlyContinue
)
if ($Listeners.Count -gt 1) {
    Write-Host "端口存在多个监听进程；为安全起见没有停止任何进程。"
    exit 1
}

$ServerPid = $RecordedPid
if ($Listeners.Count -eq 1 -and [int]$Listeners[0].OwningProcess -ne $RecordedPid) {
    $ListenerPid = [int]$Listeners[0].OwningProcess
    $ListenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$ListenerPid" `
        -ErrorAction SilentlyContinue
    if ($LauncherPid -eq 0 -and $ListenerProcess -and
        [int]$ListenerProcess.ParentProcessId -eq $RecordedPid) {
        # Compatibility with markers written by the older launcher-PID format.
        $LauncherPid = $RecordedPid
        $ServerPid = $ListenerPid
    }
    else {
        Write-Host "端口监听 PID 与运行标记不匹配；为安全起见没有停止任何进程。"
        exit 1
    }
}

$Process = Get-CimInstance Win32_Process -Filter "ProcessId=$ServerPid" `
    -ErrorAction SilentlyContinue
$Command = if ($Process) { [string]$Process.CommandLine } else { "" }
$PortPattern = '--port\s+{0}(?:\s|$)' -f [regex]::Escape([string]$ServerPort)
if (-not $Process -or $Process.Name -notmatch '^python(w)?\.exe$' -or
    $Command -notmatch 'uvicorn\s+app\.api:app' -or
    $Command -notmatch $PortPattern) {
    Write-Host "PID与本系统启动命令不匹配；为安全起见没有停止该进程。"
    exit 1
}

if ($Listeners.Count -eq 1 -and [int]$Listeners[0].OwningProcess -ne $ServerPid) {
    Write-Host "运行标记进程并非端口监听者；为安全起见没有停止任何进程。"
    exit 1
}

if ($LauncherPid -ne 0 -and $LauncherPid -ne $ServerPid) {
    $Launcher = Get-CimInstance Win32_Process -Filter "ProcessId=$LauncherPid" `
        -ErrorAction SilentlyContinue
    $ExpectedLauncher = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if ($Launcher) {
        $LauncherPath = [System.IO.Path]::GetFullPath([string]$Launcher.ExecutablePath)
        if ($LauncherPath -ne [System.IO.Path]::GetFullPath($ExpectedLauncher) -or
            [int]$Process.ParentProcessId -ne $LauncherPid) {
            Write-Host "启动器与监听进程父子关系不匹配；为安全起见没有停止任何进程。"
            exit 1
        }
    }
}

Stop-Process -Id $ServerPid -ErrorAction Stop
if ($LauncherPid -ne 0 -and $LauncherPid -ne $ServerPid -and
    (Get-Process -Id $LauncherPid -ErrorAction SilentlyContinue)) {
    Stop-Process -Id $LauncherPid -ErrorAction Stop
}
Remove-Item -LiteralPath $PidFile -Force
Write-Host "已停止本机外检计量证书智能审核网站。"
