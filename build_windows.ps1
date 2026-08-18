param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = $ProjectRoot
$env:PYTHONNOUSERSITE = "1"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "未找到隔离环境：$Python。请先创建 .venv 并安装 requirements-build.txt 或锁定依赖。"
}

& $Python -c "import webview, PyInstaller" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "隔离环境缺少桌面打包依赖，请安装 requirements-build.txt 或锁定依赖"
}

if (-not $SkipTests) {
    $env:CERT_MODEL_MODE = "disabled"
    & $Python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "测试未通过，停止打包" }
}

& $Python -m PyInstaller --noconfirm --clean .\certificate_review_desktop.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

$Executable = Join-Path $ProjectRoot "dist\证书报告批量审查\证书报告批量审查.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "打包结束但未找到可执行程序"
}

Write-Host "打包完成：$Executable"
