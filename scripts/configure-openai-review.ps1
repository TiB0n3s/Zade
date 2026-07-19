[CmdletBinding()]
param(
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$secureKey = Read-Host "OpenAI API key" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
$plainKey = $null

try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ([string]::IsNullOrWhiteSpace($plainKey) -or $plainKey.Length -lt 20) {
        throw "The supplied OpenAI API key is empty or unexpectedly short."
    }
    [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $plainKey, "User")
    $env:OPENAI_API_KEY = $plainKey

    if ($Restart) {
        & (Join-Path $PSScriptRoot "stop.ps1")
        & (Join-Path $PSScriptRoot "start.ps1")
    }
} finally {
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    $plainKey = $null
    $secureKey = $null
}

Write-Host "OPENAI_API_KEY is stored in the Windows user environment. The key was not printed or written to the repository."
if (-not $Restart) {
    Write-Host "Restart Zade with .\scripts\stop.ps1 followed by .\scripts\start.ps1."
}
