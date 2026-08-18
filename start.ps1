param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8766,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = $ProjectRoot
$env:PYTHONNOUSERSITE = "1"
$DataRoot = Join-Path $ProjectRoot "data"
$PidFile = Join-Path $DataRoot "server.pid"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if ($HostAddress -ne "127.0.0.1") {
    throw "安全策略只允许监听 127.0.0.1，拒绝地址：$HostAddress"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "未找到隔离环境：$Python。请先按 README 创建 .venv 并安装锁定依赖。"
}
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null

$Existing = Get-NetTCPConnection -LocalAddress $HostAddress -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($Existing) {
    Write-Host "本机端口 ${Port} 已有服务监听，请先双击“停止网站.cmd”。"
    exit 1
}

Write-Host "外检计量证书智能审核系统已启动：http://${HostAddress}:${Port}"
Write-Host "按 Ctrl+C 停止。默认仅监听本机；只有配置新凭据并启用模型后才会调用公司接口。"
$Launcher = Start-Process -FilePath $Python -ArgumentList @(
    "-m", "uvicorn", "app.api:app", "--host", $HostAddress, "--port", "$Port", "--no-access-log"
) -WorkingDirectory $ProjectRoot -NoNewWindow -PassThru
$ServerPid = 0
$Ready = $false
try {
    for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
        if ($Launcher.HasExited) {
            throw "本地服务进程在启动完成前退出，退出码：$($Launcher.ExitCode)"
        }
        $Listeners = @(
            Get-NetTCPConnection -LocalAddress $HostAddress -LocalPort $Port -State Listen `
                -ErrorAction SilentlyContinue
        )
        if ($Listeners.Count -gt 1) {
            throw "发现多个进程监听本机端口 ${Port}，拒绝写入运行标记"
        }
        if ($Listeners.Count -eq 1) {
            $CandidatePid = [int]$Listeners[0].OwningProcess
            $Candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$CandidatePid"
            $PortPattern = '--port\s+{0}(?:\s|$)' -f [regex]::Escape([string]$Port)
            $Command = if ($Candidate) { [string]$Candidate.CommandLine } else { "" }
            $OwnedByLauncher = $CandidatePid -eq $Launcher.Id -or (
                $Candidate -and [int]$Candidate.ParentProcessId -eq $Launcher.Id
            )
            if (-not $OwnedByLauncher -or $Candidate.Name -notmatch '^python(w)?\.exe$' -or
                $Command -notmatch 'uvicorn\s+app\.api:app' -or $Command -notmatch $PortPattern) {
                throw "端口监听进程与本次启动命令不匹配，拒绝写入运行标记"
            }
            try {
                $Health = Invoke-WebRequest -UseBasicParsing `
                    -Uri "http://${HostAddress}:${Port}/api/capabilities" -TimeoutSec 1
                if ($Health.StatusCode -eq 200) {
                    $ServerPid = $CandidatePid
                    @{
                        pid = $ServerPid
                        launcher_pid = $Launcher.Id
                        port = $Port
                        project = $ProjectRoot
                    } | ConvertTo-Json -Compress | Set-Content -LiteralPath $PidFile -Encoding utf8
                    if (-not $NoBrowser) { Start-Process "http://${HostAddress}:${Port}" }
                    $Ready = $true
                    break
                }
            }
            catch { }
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $Ready) {
        throw "本地服务未能在规定时间内通过健康检查"
    }
    Wait-Process -Id $Launcher.Id
}
catch {
    $Children = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                [int]$_.ParentProcessId -eq $Launcher.Id -and
                [string]$_.CommandLine -match 'uvicorn\s+app\.api:app'
            }
    )
    foreach ($Child in $Children) {
        Stop-Process -Id ([int]$Child.ProcessId) -ErrorAction SilentlyContinue
    }
    if (Get-Process -Id $Launcher.Id -ErrorAction SilentlyContinue) {
        Stop-Process -Id $Launcher.Id -ErrorAction SilentlyContinue
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $PidFile) {
        $Recorded = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
        if ([int]$Recorded.pid -eq $ServerPid -and [int]$Recorded.launcher_pid -eq $Launcher.Id) {
            Remove-Item -LiteralPath $PidFile -Force
        }
    }
}
