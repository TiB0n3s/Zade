# Shared mutation-token bootstrap for Zade's operational scripts.
#
# Since 2026-07-12 every mutating kernel endpoint requires X-Zade-Token, and the
# scheduled scripts (cadence, backups, trading brief, benchmark, smoke) silently
# 401'd for a week because $env:COFOUNDER_LOCAL_TOKEN was set nowhere. Instead
# of planting the token in the environment (stale-env snapshots already bit the
# Telegram adapter), resolve it the way the UI does: GET /session/token, which
# the kernel only serves on a loopback bind, to loopback callers.
#
# Precedence: an explicit -Token argument or COFOUNDER_LOCAL_TOKEN env var wins;
# otherwise bootstrap from the kernel. Fails loud with an actionable message —
# a silent empty token just moves the failure to an opaque 401 later.
function Resolve-ZadeToken {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [string]$Token
    )
    if ($Token) {
        return $Token
    }
    try {
        $Bootstrap = Invoke-RestMethod -Uri "$($BaseUrl.TrimEnd('/'))/session/token" -TimeoutSec 5
    } catch {
        throw "Could not bootstrap the Zade mutation token from $BaseUrl/session/token (and COFOUNDER_LOCAL_TOKEN is not set): $($_.Exception.Message)"
    }
    if ($Bootstrap.required -and -not $Bootstrap.token) {
        throw "Kernel at $BaseUrl requires a mutation token but /session/token returned none."
    }
    return [string]$Bootstrap.token
}
