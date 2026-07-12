"""Shared test fixtures.

Mutations are protected-by-default in production: when no token is configured,
the kernel bootstraps and persists a random one (RC1). The functional test
suite, however, models a *trusted local client* and is not exercising auth — so
here we make the bootstrap a no-op and let each test decide. A test that wants
protection simply configures ``SecurityConfig(local_token=...)``; auth-specific
behavior is covered explicitly in ``test_ui_security.py``.
"""
import pytest

from cofounder_kernel import api


@pytest.fixture(autouse=True)
def _no_bootstrap_token(monkeypatch):
    # Return only an explicitly-configured token (empty by default) — never mint
    # one — so functional tests POST without a token exactly as before.
    monkeypatch.setattr(api, "_resolve_local_token", lambda cfg: cfg.security.local_token)
