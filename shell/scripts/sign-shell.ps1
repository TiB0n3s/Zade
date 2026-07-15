<#
Sign the built Zade shell exe + NSIS installer.

Self-signed by default (the pipeline is real; the trust is not — SmartScreen still
warns on other machines unless the cert is installed to Trusted Publishers). For a
real OV/EV cert, set ZADE_SIGN_THUMBPRINT to its thumbprint and it signs with that.

Run AFTER `npx tauri build`.
#>
param([string]$Thumbprint = $env:ZADE_SIGN_THUMBPRINT)

$ErrorActionPreference = "Stop"
$shellRoot = Split-Path $PSScriptRoot -Parent
$release = Join-Path $shellRoot "src-tauri\target\release"

if ($Thumbprint) {
    $cert = Get-Item "Cert:\CurrentUser\My\$Thumbprint" -ErrorAction SilentlyContinue
} else {
    $cert = Get-ChildItem Cert:\CurrentUser\My |
        Where-Object { $_.Subject -eq 'CN=Zade Local Shell' } | Select-Object -First 1
}
if (-not $cert) {
    throw "No signing certificate. Set ZADE_SIGN_THUMBPRINT to a real cert, or create the self-signed 'CN=Zade Local Shell' code-signing cert first."
}

$targets = @()
$exe = Join-Path $release "zade-shell.exe"
if (Test-Path $exe) { $targets += $exe }
Get-ChildItem (Join-Path $release "bundle\nsis") -Filter "Zade_*-setup.exe" -ErrorAction SilentlyContinue |
    ForEach-Object { $targets += $_.FullName }

if (-not $targets) { throw "Nothing to sign under $release. Run 'npx tauri build' first." }

foreach ($file in $targets) {
    $result = Set-AuthenticodeSignature -FilePath $file -Certificate $cert -HashAlgorithm SHA256 `
        -TimestampServer "http://timestamp.digicert.com"
    Write-Output ("{0,-12} {1}" -f $result.Status, $file)
}
