$ErrorActionPreference = "Stop"

# Launch the resident Zade desktop tray using the project venv. The tray polls
# the kernel over loopback and shows status + OS toasts; it is read-only.
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Python)) {
    # Fall back to python.exe if the windowless interpreter is absent.
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
}

& $Python -m cofounder_kernel.tray
