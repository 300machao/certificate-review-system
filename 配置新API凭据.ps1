$ErrorActionPreference = "Stop"

function Read-Secret([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

$qwenKey = Read-Secret "输入新生成的千问API凭据"
$glmKey = Read-Secret "输入新生成的GLM API凭据"
$arbiterKey = Read-Secret "输入新生成的DeepSeek仲裁API凭据"

try {
    [Environment]::SetEnvironmentVariable("CERT_QWEN_API_KEY", $qwenKey, "User")
    [Environment]::SetEnvironmentVariable("CERT_GLM_API_KEY", $glmKey, "User")
    [Environment]::SetEnvironmentVariable("CERT_ARBITER_API_KEY", $arbiterKey, "User")
}
finally {
    $qwenKey = $null
    $glmKey = $null
    $arbiterKey = $null
}

Write-Host "三枚新凭据已写入当前 Windows 用户环境变量。"
Write-Host "请关闭并重新启动网站。不要把凭据写进聊天、代码、配置文件或截图。"
