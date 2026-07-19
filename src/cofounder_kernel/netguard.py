"""Central egress policy.

Every outbound HTTP request the kernel makes funnels through ``assert_allowed``
so one SSRF/allowlist review covers all of them, instead of four ad-hoc checks
drifting apart. The kernel is local-first and talks to very few hosts:

  * the local Ollama server (loopback)                   -> allow_private
  * an optional founder calendar/ICS feed (public https) -> allow_loopback_http+require_https
  * the founder's own SMS gateway (often a LAN device)   -> allow_private
  * fixed cloud APIs pinned to known hosts               -> require_https + allowed_hosts

Call sites declare intent (allow_private, require_https, allowed_hosts) and this
module enforces it. DNS is resolved (fail-closed) so a public hostname cannot
resolve to an internal address and slip past a name-only check.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
import urllib.request
from typing import Any

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class EgressError(ValueError):
    """An outbound request was refused by egress policy."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None  # refuse all redirects; a 3xx becomes an HTTPError


# Shared opener that refuses redirects, so a validated public URL cannot 3xx-hop
# to an internal service after the check has passed.
NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def is_private_host(host: str) -> bool:
    """True if *host* is loopback/private/link-local/reserved/multicast, or does
    not resolve. Unresolvable is treated as private so we fail closed."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def assert_allowed(
    url: str,
    *,
    allow_private: bool = False,
    allow_loopback_http: bool = False,
    require_https: bool = False,
    allowed_hosts: frozenset[str] | set[str] | None = None,
) -> urllib.parse.ParseResult:
    """Enforce egress policy for *url*; return the parsed URL or raise EgressError.

    allow_private       permit private/LAN/loopback targets (local Ollama, SMS gateway)
    allow_loopback_http permit plain http, but only to a loopback host
    require_https       refuse anything but https (fixed cloud APIs)
    allowed_hosts       if given, host must be in this set (case-insensitive)
    """
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise EgressError(f"Refused egress: scheme must be http/https, got {scheme!r}.")
    if not host:
        raise EgressError("Refused egress: URL has no host.")

    is_loopback = host in _LOOPBACK_HOSTS
    loopback_http_ok = allow_loopback_http and is_loopback and scheme == "http"

    if scheme == "http" and not allow_private and not loopback_http_ok:
        raise EgressError(f"Refused egress: http is only allowed to loopback, got {host!r}.")
    if require_https and scheme != "https" and not loopback_http_ok:
        raise EgressError(f"Refused egress: https required, got {scheme!r}.")
    if allowed_hosts is not None and host not in {h.lower() for h in allowed_hosts}:
        raise EgressError(f"Refused egress: host {host!r} is not in the allowlist.")
    if not allow_private and not loopback_http_ok and is_private_host(host):
        raise EgressError(f"Refused egress: {host!r} resolves to a private/internal address.")
    return parsed
