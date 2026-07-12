"""Stage 5: coverage for the safety-critical deny paths the audit found untested."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient
from cofounder_kernel.trading_bot import _normalize_dt_trigger_proposal, _validate_sqlite_read_sql


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def test_trading_sqlite_validator_allows_reads_blocks_writes() -> None:
    # Read-only statements are accepted.
    for ok in (
        "SELECT * FROM dt_recommendations",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "EXPLAIN SELECT 1",
        "PRAGMA table_info(dt_recommendations)",
    ):
        assert _validate_sqlite_read_sql(ok)

    # Every mutation / escalation token is blocked.
    for bad in (
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN c TEXT",
        "ATTACH DATABASE 'x.db' AS y",
        "SELECT load_extension('evil')",
        "CREATE TABLE t (a INT)",
        "VACUUM",
    ):
        with pytest.raises(ValueError):
            _validate_sqlite_read_sql(bad)


def test_trading_sqlite_validator_anti_bypass() -> None:
    # Stacked statements are rejected...
    with pytest.raises(ValueError):
        _validate_sqlite_read_sql("SELECT 1; DROP TABLE t")
    # ...including one smuggled after a block comment (comment masking preserves the ';').
    with pytest.raises(ValueError):
        _validate_sqlite_read_sql("SELECT 1 /* c */; DROP TABLE t")
    # A write token INSIDE a string literal is inert and correctly allowed.
    assert _validate_sqlite_read_sql("SELECT 'DROP TABLE t' AS note")
    # A write token inside a line comment is neutralized (statement is just SELECT 1).
    assert _validate_sqlite_read_sql("SELECT 1 -- DROP TABLE t\n")


def test_dt_trigger_proposal_denies_live_trading_boundary() -> None:
    for op in ("place order for TSLA", "execute a LIVE TRADE", "call the broker", "increase account risk"):
        with pytest.raises(ValueError) as exc:
            _normalize_dt_trigger_proposal({"operation": op, "reason": "x"})
        assert "denied live-trading boundary" in str(exc.value)

    # A genuine observe-only proposal is accepted.
    ok = _normalize_dt_trigger_proposal({"operation": "review yesterday's fill quality report", "reason": "learning"})
    assert ok["operation"].startswith("review")


def test_denied_work_item_cannot_be_dispatched(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    app = create_app(config)
    db = app.state.db
    approvals = app.state.approvals

    # Force the defense-in-depth state: an item marked approved but carrying a deny decision.
    item_id, _ = db.enqueue_work_item(
        kind="external", title="should never run", detail="", action="local.noop", target="",
        permission_tier="L3_EXTERNAL_ACTION", priority=50, source="test", metadata={},
    )
    db.update_work_item(item_id, status="approved", authority_decision="deny")

    with pytest.raises(ValueError) as exc:
        approvals.dispatch_work_item(item_id, typed_confirmation="make the jump to hyperspace")
    assert "Denied or irreversible" in str(exc.value)
