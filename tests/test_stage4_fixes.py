"""Stage 4 (RC3 + P2) regression tests: migrations, atomic claim, timeout wrapping."""
from pathlib import Path

import pytest

from cofounder_kernel.config import OllamaConfig
from cofounder_kernel.db import SCHEMA_VERSION, KernelDatabase
from cofounder_kernel.ollama import OllamaClient, OllamaError


def _db(tmp_path: Path) -> KernelDatabase:
    db = KernelDatabase(tmp_path / "k.sqlite")
    db.migrate()
    return db


def test_migrate_is_version_aware_and_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    assert db.schema_version() == SCHEMA_VERSION
    # Running migrate again is safe and keeps the version.
    db.migrate()
    assert db.schema_version() == SCHEMA_VERSION


def test_add_column_if_missing_is_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with db.connect() as conn:
        conn.execute("CREATE TABLE t_patch (id INTEGER PRIMARY KEY, a TEXT)")
    with db.connect() as conn:
        added = db._add_column_if_missing(conn, "t_patch", "b", "b TEXT NOT NULL DEFAULT ''")
        assert added is True
    with db.connect() as conn:
        again = db._add_column_if_missing(conn, "t_patch", "b", "b TEXT NOT NULL DEFAULT ''")
        assert again is False
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(t_patch)").fetchall()}
        assert "b" in cols
    # A patch targeting a non-existent table is a no-op, never an error.
    with db.connect() as conn:
        assert db._add_column_if_missing(conn, "no_such_table", "x", "x TEXT") is False


def test_claim_next_work_item_is_atomic(tmp_path: Path) -> None:
    db = _db(tmp_path)
    ids = []
    for i in range(3):
        item_id, _ = db.enqueue_work_item(
            kind="test", title=f"item {i}", detail="", action="local.noop", target="",
            permission_tier="L1_MEMORY_WRITE", priority=50, source="test", metadata={},
        )
        db.update_work_item(item_id, status="pending", authority_decision="allow")
        ids.append(item_id)

    first = db.claim_next_work_item()
    second = db.claim_next_work_item()
    third = db.claim_next_work_item()
    fourth = db.claim_next_work_item()

    assert first is not None and second is not None and third is not None
    # Every claim returns a distinct item, already marked running.
    claimed = {first.id, second.id, third.id}
    assert claimed == set(ids)
    assert first.status == "running"
    # Nothing pending remains.
    assert fourth is None


def test_ollama_timeout_becomes_ollama_error(tmp_path: Path, monkeypatch) -> None:
    client = OllamaClient(OllamaConfig(base_url="http://127.0.0.1:1"))

    def raise_timeout(*args, **kwargs):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("cofounder_kernel.ollama.urllib.request.urlopen", raise_timeout)
    with pytest.raises(OllamaError):
        client.generate(prompt="hi", model="qwen3:14b")
    with pytest.raises(OllamaError):
        client.health()
