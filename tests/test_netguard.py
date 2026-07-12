"""Central egress policy — the one chokepoint every outbound call funnels through."""
from cofounder_kernel import netguard


def _refused(url: str, **kwargs) -> bool:
    try:
        netguard.assert_allowed(url, **kwargs)
    except netguard.EgressError:
        return True
    return False


def test_scheme_and_host_are_required() -> None:
    assert _refused("ftp://example.com/x")
    assert _refused("file:///etc/passwd")
    assert _refused("https://")  # no host
    assert _refused("not-a-url")


def test_default_policy_blocks_private_and_plain_http() -> None:
    # No flags: only public https is allowed.
    assert _refused("http://example.com/")            # plain http
    assert _refused("https://10.0.0.1/")              # private
    assert _refused("https://169.254.169.254/")       # link-local metadata
    assert netguard.assert_allowed("https://example.com/ok").hostname == "example.com"


def test_allow_private_permits_lan_and_loopback() -> None:
    # SMS gateway / local Ollama: LAN + loopback allowed, but scheme still enforced.
    assert netguard.assert_allowed("http://192.168.1.50:8080/send", allow_private=True)
    assert netguard.assert_allowed("http://127.0.0.1:11434/api/generate", allow_private=True)
    assert _refused("ssh://192.168.1.50/", allow_private=True)  # scheme still checked


def test_loopback_http_carveout_matches_connectors_ics_policy() -> None:
    # connectors: https everywhere, except http on loopback for local testing.
    ok = dict(allow_loopback_http=True, require_https=True)
    assert netguard.assert_allowed("http://127.0.0.1/cal.ics", **ok)
    assert netguard.assert_allowed("http://localhost/cal.ics", **ok)
    assert _refused("http://example.com/cal.ics", **ok)          # http, non-loopback
    assert _refused("http://127.0.0.1.evil.com/cal.ics", **ok)   # look-alike host
    assert _refused("https://10.0.0.1/cal.ics", **ok)            # https but private


def test_allowed_hosts_locks_voice_egress() -> None:
    hosts = frozenset({"api.deepgram.com", "api.elevenlabs.io"})
    ok = dict(require_https=True, allowed_hosts=hosts)
    assert netguard.assert_allowed("https://api.deepgram.com/v1/listen", **ok)
    assert netguard.assert_allowed("https://api.elevenlabs.io/v1/text-to-speech/x", **ok)
    assert _refused("https://evil.example.com/v1/listen", **ok)  # off-allowlist
    assert _refused("http://api.deepgram.com/v1/listen", **ok)   # not https


def test_is_private_host_classifies_addresses() -> None:
    assert netguard.is_private_host("10.0.0.1") is True
    assert netguard.is_private_host("192.168.1.10") is True
    assert netguard.is_private_host("169.254.169.254") is True
    assert netguard.is_private_host("::1") is True
    assert netguard.is_private_host("8.8.8.8") is False
