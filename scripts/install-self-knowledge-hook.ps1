param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $VenvPython) {
    $Python = $VenvPython
} else {
    $Python = "python"
}

$Args = @(
    "-m",
    "cofounder_kernel.self_knowledge.hook_installer",
    "--repo-root",
    $Root
)
if ($Force) {
    $Args += "--force"
}

& $Python @Args
exit $LASTEXITCODE
