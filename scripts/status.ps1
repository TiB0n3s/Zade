$ErrorActionPreference = "Stop"

try {
    Invoke-RestMethod -Uri "http://127.0.0.1:8787/health" -TimeoutSec 5 | ConvertTo-Json -Depth 6
} catch {
    Write-Output "Kernel is not responding at http://127.0.0.1:8787"
    Write-Output $_.Exception.Message
}

