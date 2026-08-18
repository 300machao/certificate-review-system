$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = $ProjectRoot
$env:PYTHONNOUSERSITE = "1"
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) {
    throw "未找到隔离环境：$Pythonw。请先按 README 创建 .venv 并安装锁定依赖。"
}
& $Pythonw .\desktop_main.pyw
if ($LASTEXITCODE -ne 0) { throw "桌面程序启动失败，退出码：$LASTEXITCODE" }
