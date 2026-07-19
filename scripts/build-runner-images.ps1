[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$definitions = Join-Path $root "docker\build-runners"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is unavailable. Install Docker Desktop before building runner images."
}

docker info --format "{{.ServerVersion}}" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is installed, but its daemon is unavailable."
}

docker build --file (Join-Path $definitions "python.Dockerfile") --tag "python:3.12-local" $definitions
if ($LASTEXITCODE -ne 0) { throw "Failed to build python:3.12-local." }

docker build --file (Join-Path $definitions "node.Dockerfile") --tag "node:22-local" $definitions
if ($LASTEXITCODE -ne 0) { throw "Failed to build node:22-local." }

$sandbox = @(
    "run", "--rm",
    "--network", "none",
    "--read-only",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--pids-limit", "64",
    "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m"
)

docker @sandbox "python:3.12-local" python -m pytest --version
if ($LASTEXITCODE -ne 0) { throw "python:3.12-local failed its isolated smoke test." }

docker @sandbox "node:22-local" node --version
if ($LASTEXITCODE -ne 0) { throw "node:22-local failed its isolated Node smoke test." }

docker @sandbox "node:22-local" npm --version
if ($LASTEXITCODE -ne 0) { throw "node:22-local failed its isolated npm smoke test." }

Write-Host "Ready: python:3.12-local and node:22-local"
