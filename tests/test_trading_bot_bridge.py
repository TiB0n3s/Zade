import json
from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, TradingBotConfig
from cofounder_kernel.ollama import OllamaClient
from cofounder_kernel.trading_bot import (
    DT_RECOMMENDATION_RUNTIME_EFFECT,
    TradingBotBridge,
    ZADE_DT_RECOMMENDATION_ACTION,
    ZADE_DT_TRIGGER_PROPOSAL_ACTION,
)


PHRASE = "make the jump to hyperspace"


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        trading_bot=TradingBotConfig(wsl_distro="TestDistro", repo_path="/tmp/trading-bot", python="python3"),
    )


def test_trading_bot_recommendation_queues_approval_gated_work(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    queued = client.post(
        "/trading-bot/recommendations",
        json={
            "market_date": "2026-07-12",
            "symbol": "AAPL",
            "action": "buy",
            "verdict": "recommend",
            "conviction": 72,
            "reason": "Observed evidence supports an advisory-only paper recommendation.",
            "evidence": [{"source": "zade", "summary": "Local analysis only."}],
        },
    )
    console = client.get("/approval-console")

    assert queued.status_code == 200
    assert queued.json()["status"] == "approval_required"
    assert queued.json()["action"] == ZADE_DT_RECOMMENDATION_ACTION
    assert queued.json()["recommendation"]["runtime_effect"] == DT_RECOMMENDATION_RUNTIME_EFFECT
    item = console.json()["items"][0]
    assert item["request"]["action"] == ZADE_DT_RECOMMENDATION_ACTION
    assert item["available_actions"]["dispatch"] is True
    assert item["authority_tier"]["authority_decision"] == "approval_required"
    assert item["risk"]


def test_trading_bot_recommendation_dispatch_calls_bot_ingest_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    commands: list[str] = []

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        commands.append(script)
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": (
                '{"ingested": true, "id": 123, '
                '"runtime_effect": "advisory_only_no_trade_authority"}\n'
            ),
            "stderr": "",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    client = TestClient(create_app(_config(tmp_path)))

    queued = client.post(
        "/trading-bot/recommendations",
        json={
            "market_date": "2026-07-12",
            "symbol": "MSFT",
            "action": "hold",
            "verdict": "abstain",
            "reason": "Signal is not clean enough for an advisory stance.",
            "idempotency_key": "zade-test-0001",
        },
    )
    request_id = client.get("/approval-requests").json()["items"][0]["id"]
    approved = client.post(
        f"/approval-requests/{request_id}/approve",
        json={"dispatch": True, "typed_confirmation": PHRASE},
    )

    assert queued.status_code == 200
    assert approved.status_code == 200
    assert approved.json()["dispatch"] == "dispatched"
    assert approved.json()["dispatch_result"]["bot_result"]["ingested"] is True
    assert "scripts/dt_recommendation_ingest.py" in commands[-1]
    assert "--runtime-effect advisory_only_no_trade_authority" in commands[-1]
    assert "--idempotency-key zade-test-0001" in commands[-1]


def test_trading_bot_ops_check_is_allowlisted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    commands: list[str] = []

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        commands.append(script)
        return {"ok": True, "exit_code": 0, "stdout": "authority ok\n", "stderr": ""}

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    client = TestClient(create_app(_config(tmp_path)))

    ok = client.post("/trading-bot/ops-check", json={"command": "authority-health"})
    needs_date = client.post("/trading-bot/ops-check", json={"command": "paper-session-evidence"})
    forbidden = client.post("/trading-bot/ops-check", json={"command": "historical-bar-archive"})
    dated = client.post(
        "/trading-bot/ops-check",
        json={"command": "paper-session-evidence", "target_date": "2026-07-12"},
    )

    assert ok.status_code == 200
    assert ok.json()["runtime_effect"] == "read_only_diagnostic_no_trade_authority"
    assert "ops_check.py authority-health" in commands[0]
    assert needs_date.status_code == 400
    assert forbidden.status_code == 400
    assert dated.status_code == 200
    assert "ops_check.py paper-session-evidence 2026-07-12" in commands[-1]


def test_trading_bot_intelligence_access_profile_exposes_full_intelligence_scope(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.get("/trading-bot/intelligence/access")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_effect"] == "full_intelligence_no_broker_order_authority"
    assert body["capabilities"]["training"]["enabled"] is True
    assert "supervised-predictions" in body["capabilities"]["training"]["commands"]
    assert body["capabilities"]["advisory"]["enabled"] is True
    assert body["capabilities"]["events"]["read"] is True
    assert body["capabilities"]["market_context"]["read"] is True
    assert body["capabilities"]["signals"]["watch"] is True
    assert body["authority_boundary"]["broker_order_sizing_gate_mutation"] is False


def test_trading_bot_training_run_is_allowlisted_and_blocks_shell_injection(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    commands: list[str] = []

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        commands.append(script)
        return {"ok": True, "exit_code": 0, "stdout": "trained\n", "stderr": ""}

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    client = TestClient(create_app(_config(tmp_path)))

    ok = client.post(
        "/trading-bot/training/run",
        json={
            "command": "supervised-predictions",
            "symbols": ["AAPL"],
            "extra_args": ["--json"],
        },
    )
    unsupported = client.post("/trading-bot/training/run", json={"command": "live-trader"})
    injected = client.post(
        "/trading-bot/training/run",
        json={"command": "supervised-predictions", "extra_args": ["; rm -rf /"]},
    )

    assert ok.status_code == 200
    assert ok.json()["runtime_effect"] == "full_intelligence_no_broker_order_authority"
    assert "scripts/train_supervised_predictions.py" in commands[0]
    assert "--symbol AAPL" in commands[0]
    assert "--json" in commands[0]
    assert unsupported.status_code == 400
    assert injected.status_code == 400
    assert len(commands) == 1


def test_trading_bot_recent_events_reads_bot_event_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    commands: list[str] = []

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        commands.append(script)
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": json.dumps(
                [{"id": 7, "event_type": "signal", "symbol": "AAPL", "created_at": "2026-07-12T14:30:00"}]
            ),
            "stderr": "",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.get("/trading-bot/events/recent?limit=2&symbol=AAPL&event_type=signal")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_effect"] == "full_intelligence_no_broker_order_authority"
    assert body["items"][0]["event_type"] == "signal"
    assert "scripts/bot_events.py" in commands[0]
    assert "--json" in commands[0]
    assert "--limit 2" in commands[0]
    assert "--symbol AAPL" in commands[0]
    assert "--event-type signal" in commands[0]


def test_trading_bot_signal_watch_reads_signal_database_tables(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    queries: list[str] = []

    def fake_query(self: TradingBotBridge, **kwargs) -> dict:
        sql = kwargs["sql"]
        queries.append(sql)
        if "webhook_events" in sql:
            rows = [{"id": 1, "symbol": "AAPL", "event_type": "webhook", "received_at": "2026-07-12T14:30:00"}]
        elif "auto_buy_decision_snapshots" in sql:
            rows = [{"id": 2, "symbol": "AAPL", "decision": "approved", "created_at": "2026-07-12T14:31:00"}]
        else:
            rows = []
        return {
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
            "runtime_effect": "read_only_sqlite_no_trade_authority",
        }

    monkeypatch.setattr(TradingBotBridge, "run_sqlite_query", fake_query)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.get("/trading-bot/signals/recent?limit=3&symbol=AAPL")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_effect"] == "full_intelligence_no_broker_order_authority"
    assert body["summary"]["total_rows"] == 2
    assert body["tables"]["webhook_events"]["rows"][0]["symbol"] == "AAPL"
    assert body["tables"]["auto_buy_decision_snapshots"]["rows"][0]["decision"] == "approved"
    assert any("webhook_events" in query for query in queries)
    assert any("auto_buy_decision_snapshots" in query for query in queries)
    assert body["authority_boundary"]["broker_order_sizing_gate_mutation"] is False


def test_trading_bot_market_context_reads_file_and_context_tables(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        assert "market_context.json" in script
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": json.dumps({"exists": True, "path": "market_context.json", "data": {"regime": "risk-on"}}),
            "stderr": "",
        }

    def fake_query(self: TradingBotBridge, **kwargs) -> dict:
        rows = [{"id": 1, "symbol": "AAPL", "context_date": "2026-07-12", "summary": "earnings drift"}]
        return {
            "columns": list(rows[0].keys()),
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
            "runtime_effect": "read_only_sqlite_no_trade_authority",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    monkeypatch.setattr(TradingBotBridge, "run_sqlite_query", fake_query)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.get("/trading-bot/market-context?target_date=2026-07-12&symbol=AAPL&limit=5")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_effect"] == "full_intelligence_no_broker_order_authority"
    assert body["market_context_file"]["data"]["regime"] == "risk-on"
    assert body["tables"]["daily_symbol_context"]["rows"][0]["summary"] == "earnings drift"
    assert body["authority_boundary"]["broker_order_sizing_gate_mutation"] is False


def test_trading_bot_advisory_generate_queues_diagnostic_recommendation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        assert "authority-health" in script
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": (
                "report_version             : authority_health_v1\n"
                "runtime_effect             : diagnostic_only_no_live_authority\n"
                "authority_clean            : True\n"
                "AAPL approved candidate_ready\n"
                "[OK] authority checks are clean\n"
            ),
            "stderr": "",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    client = TestClient(create_app(_config(tmp_path)))

    generated = client.post(
        "/trading-bot/advisory/generate",
        json={
            "target_date": "2026-07-12",
            "symbols": ["AAPL"],
            "queue": True,
            "include_ops_checks": ["authority-health"],
            "use_sqlite_snapshot": False,
        },
    )
    console = client.get("/approval-console")

    assert generated.status_code == 200
    body = generated.json()
    assert body["evidence"]["evidence_id"]
    assert body["evidence"]["memory_id"]
    assert body["recommendations"][0]["symbol"] == "AAPL"
    assert body["recommendations"][0]["runtime_effect"] == DT_RECOMMENDATION_RUNTIME_EFFECT
    assert body["queued"][0]["status"] == "approval_required"
    assert console.json()["items"][0]["request"]["action"] == ZADE_DT_RECOMMENDATION_ACTION


def test_trading_bot_advisory_generate_does_not_invent_symbols(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": (
                "report_version         : shadow_prediction_run_v1\n"
                "runtime_effect         : observe_only_no_live_authority\n"
                "rows                   : 0\n"
                "[WARN] no shadow prediction rows found\n"
            ),
            "stderr": "",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    client = TestClient(create_app(_config(tmp_path)))

    generated = client.post(
        "/trading-bot/advisory/generate",
        json={
            "target_date": "2026-07-12",
            "queue": True,
            "include_ops_checks": ["shadow-predictions"],
            "use_sqlite_snapshot": False,
        },
    )

    assert generated.status_code == 200
    body = generated.json()
    assert body["recommendations"] == []
    assert body["queued"] == []
    assert body["skipped"][0]["reason"].startswith("No safe symbol target")
    assert body["evidence"]["evidence_id"]


def test_trading_bot_advisory_score_records_outcome_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        if "dt-recommendation-outcomes" in script:
            stdout = (
                "runtime_effect          : read_only_diagnostic_no_trade_authority\n"
                "recommendations         : 2\n"
                "  recommend    1 +1.000 +0.012 +0.031\n"
                "  abstain      1 - - -\n"
                "  AAPL   buy  recommend decisions=1 approved=1 agree=True realized=+0.012 cf60m=+0.031\n"
            )
        else:
            stdout = (
                "runtime_effect          : advisory_only_no_trade_authority\n"
                "recommendations         : 2\n"
            )
        return {"ok": True, "exit_code": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    client = TestClient(create_app(_config(tmp_path)))

    scored = client.post("/trading-bot/advisory/score", json={"target_date": "2026-07-12"})

    assert scored.status_code == 200
    body = scored.json()
    assert body["runtime_effect"] == "read_only_diagnostic_no_trade_authority"
    assert body["scorecard"]["recommendations"] == 2
    assert body["scorecard"]["by_verdict"]["recommend"]["agreement_rate"] == 1.0
    assert body["evidence"]["evidence_id"]


def test_trading_bot_deep_thought_replacement_map_is_exposed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.get("/trading-bot/deep-thought-replacement")

    assert response.status_code == 200
    body = response.json()
    assert body["active_count"] >= 6
    assert body["planned_count"] == 0
    assert any("BotBridge.query_sqlite" in seam["deep_thought_integration"] for seam in body["seams"])
    assert any("dt_trigger" in seam["deep_thought_integration"] and seam["status"] == "active" for seam in body["seams"])


def test_trading_bot_sqlite_query_blocks_mutation_before_worker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    calls: list[str] = []

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        calls.append(script)
        return {"ok": True, "exit_code": 0, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/trading-bot/sqlite/query",
        json={"sql": "UPDATE trades SET approved = 1", "limit": 10},
    )

    assert response.status_code == 400
    assert calls == []


def test_trading_bot_sqlite_query_runs_readonly_worker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    commands: list[str] = []

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        commands.append(script)
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": json.dumps(
                {
                    "ok": True,
                    "database": "/tmp/trading-bot/trades.db",
                    "query_only": True,
                    "columns": ["symbol", "score"],
                    "rows": [{"symbol": "AAPL", "score": 72.5}],
                    "row_count": 1,
                    "truncated": False,
                }
            ),
            "stderr": "",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/trading-bot/sqlite/query",
        json={
            "sql": "SELECT symbol, score FROM auto_buy_candidates WHERE symbol = ?",
            "params": ["AAPL"],
            "limit": 5,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_effect"] == "read_only_sqlite_no_trade_authority"
    assert body["query_only"] is True
    assert body["rows"] == [{"symbol": "AAPL", "score": 72.5}]
    assert "PRAGMA query_only = ON" in commands[0]


def test_trading_bot_evidence_snapshot_records_table_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_query(self: TradingBotBridge, **kwargs) -> dict:
        assert kwargs["params"][0] == "2026-07-12"
        return {
            "columns": ["id", "timestamp", "symbol", "decision", "score", "order_submitted"],
            "rows": [
                {
                    "id": 1,
                    "timestamp": "2026-07-12T14:30:00",
                    "symbol": "AAPL",
                    "decision": "approved",
                    "score": 71.0,
                    "order_submitted": 1,
                }
            ],
            "row_count": 1,
            "truncated": False,
            "runtime_effect": "read_only_sqlite_no_trade_authority",
        }

    monkeypatch.setattr(TradingBotBridge, "run_sqlite_query", fake_query)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/trading-bot/evidence/snapshot",
        json={
            "target_date": "2026-07-12",
            "symbols": ["AAPL"],
            "tables": ["auto_buy_candidates"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["total_rows"] == 1
    assert body["summary"]["symbols"] == ["AAPL"]
    assert body["evidence"]["evidence_id"]


def test_trading_bot_advisory_generate_uses_sqlite_snapshot_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": (
                "report_version             : authority_health_v1\n"
                "runtime_effect             : diagnostic_only_no_live_authority\n"
                "authority_clean            : True\n"
                "[OK] authority checks are clean\n"
            ),
            "stderr": "",
        }

    def fake_query(self: TradingBotBridge, **kwargs) -> dict:
        sql = kwargs["sql"]
        rows = []
        if "auto_buy_candidates" in sql:
            rows = [
                {
                    "id": 1,
                    "timestamp": "2026-07-12T14:30:00",
                    "symbol": "AAPL",
                    "decision": "approved",
                    "score": 73.0,
                    "order_submitted": 1,
                    "reason": "wealth_engine_approved: order submitted",
                }
            ]
        return {
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
            "runtime_effect": "read_only_sqlite_no_trade_authority",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    monkeypatch.setattr(TradingBotBridge, "run_sqlite_query", fake_query)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/trading-bot/advisory/generate",
        json={
            "target_date": "2026-07-12",
            "symbols": ["AAPL"],
            "queue": False,
            "include_ops_checks": ["authority-health"],
            "snapshot_tables": ["auto_buy_candidates"],
        },
    )

    assert response.status_code == 200
    recommendation = response.json()["recommendations"][0]
    assert recommendation["symbol"] == "AAPL"
    assert recommendation["action"] == "buy"
    assert recommendation["verdict"] == "recommend"
    assert recommendation["evidence"][0]["sqlite_rows"]["auto_buy_candidates"][0]["score"] == 73.0


def test_trading_bot_daily_brief_records_and_scores_judgment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        if "dt-recommendation-outcomes" in script:
            stdout = (
                "recommendations              : 1\n"
                "recommend 1 1.0 0.02 0.01\n"
                "AAPL buy recommend realized_return=0.03 agree=True\n"
            )
        else:
            stdout = (
                "report_version             : authority_health_v1\n"
                "runtime_effect             : diagnostic_only_no_live_authority\n"
                "authority_clean            : True\n"
                "[OK] authority checks are clean\n"
            )
        return {"ok": True, "exit_code": 0, "stdout": stdout, "stderr": ""}

    def fake_query(self: TradingBotBridge, **kwargs) -> dict:
        rows = [
            {
                "id": 1,
                "timestamp": "2026-07-12T14:30:00",
                "symbol": "AAPL",
                "decision": "approved",
                "score": 74.0,
                "order_submitted": 1,
                "reason": "wealth_engine_approved: order submitted",
            }
        ]
        return {
            "columns": list(rows[0].keys()),
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
            "runtime_effect": "read_only_sqlite_no_trade_authority",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    monkeypatch.setattr(TradingBotBridge, "run_sqlite_query", fake_query)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/trading-bot/daily-brief",
        json={
            "target_date": "2026-07-12",
            "symbols": ["AAPL"],
            "snapshot_tables": ["auto_buy_candidates"],
            "include_ops_checks": ["authority-health"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["brief"]["counts"]["strong"] == 1
    assert body["evidence"]["evidence_id"]
    assert body["advisory_candidates"][0]["symbol"] == "AAPL"
    assert body["judgments"][0]["symbol"] == "AAPL"
    assert body["score_updates"][0]["outcome_status"] == "hit"

    judgments = client.get("/trading-bot/judgments?market_date=2026-07-12&symbol=AAPL").json()["items"]
    assert len(judgments) == 1
    assert judgments[0]["outcome_status"] == "hit"


def test_trading_bot_daily_brief_creates_missed_call_for_bad_judgment_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        if "dt-recommendation-outcomes" in script:
            stdout = (
                "recommendations              : 1\n"
                "recommend 1 0.0 -0.02 0.03\n"
                "AAPL buy recommend realized_return=-0.02 agree=False\n"
            )
        else:
            stdout = (
                "report_version             : authority_health_v1\n"
                "runtime_effect             : diagnostic_only_no_live_authority\n"
                "authority_clean            : True\n"
                "[OK] authority checks are clean\n"
            )
        return {"ok": True, "exit_code": 0, "stdout": stdout, "stderr": ""}

    def fake_query(self: TradingBotBridge, **kwargs) -> dict:
        rows = [
            {
                "id": 1,
                "timestamp": "2026-07-12T14:30:00",
                "symbol": "AAPL",
                "decision": "approved",
                "score": 74.0,
                "order_submitted": 1,
                "reason": "wealth_engine_approved: order submitted",
            }
        ]
        return {
            "columns": list(rows[0].keys()),
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
            "runtime_effect": "read_only_sqlite_no_trade_authority",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    monkeypatch.setattr(TradingBotBridge, "run_sqlite_query", fake_query)
    client = TestClient(create_app(_config(tmp_path)))
    payload = {
        "target_date": "2026-07-12",
        "symbols": ["AAPL"],
        "snapshot_tables": ["auto_buy_candidates"],
        "include_ops_checks": ["authority-health"],
    }

    first = client.post("/trading-bot/daily-brief", json=payload).json()
    second = client.post("/trading-bot/daily-brief", json=payload).json()

    assert first["score_updates"][0]["outcome_status"] == "miss"
    assert first["missed_calls"][0]["error_type"] == "trading_advisory_miss"
    assert second["missed_calls"] == []


def test_trading_bot_direct_score_uses_realized_outcome_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": (
                "report_version             : authority_health_v1\n"
                "runtime_effect             : diagnostic_only_no_live_authority\n"
                "authority_clean            : True\n"
                "[OK] authority checks are clean\n"
            ),
            "stderr": "",
        }

    def fake_query(self: TradingBotBridge, **kwargs) -> dict:
        sql = kwargs["sql"]
        if "rejected_signal_outcomes" in sql:
            rows = [
                {
                    "id": 9,
                    "timestamp": "2026-07-12T15:00:00",
                    "symbol": "AAPL",
                    "action": "buy",
                    "rejection_reason": "test_outcome",
                    "return_60m": -0.031,
                    "return_eod": -0.025,
                    "label_status": "labeled",
                }
            ]
        elif "trades" in sql:
            rows = []
        else:
            rows = [
                {
                    "id": 1,
                    "timestamp": "2026-07-12T14:30:00",
                    "symbol": "AAPL",
                    "decision": "approved",
                    "score": 74.0,
                    "order_submitted": 1,
                    "reason": "wealth_engine_approved: order submitted",
                }
            ]
        return {
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
            "runtime_effect": "read_only_sqlite_no_trade_authority",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    monkeypatch.setattr(TradingBotBridge, "run_sqlite_query", fake_query)
    client = TestClient(create_app(_config(tmp_path)))
    client.post(
        "/trading-bot/daily-brief",
        json={
            "target_date": "2026-07-12",
            "symbols": ["AAPL"],
            "snapshot_tables": ["auto_buy_candidates"],
            "include_ops_checks": ["authority-health"],
            "score_outcomes": False,
        },
    )

    invalid_scope = client.post(
        "/trading-bot/judgments/score",
        json={"target_date": "2026-07-12", "symbols": ["__NO_SUCH_SYMBOL__"]},
    )
    scored = client.post("/trading-bot/judgments/score", json={"target_date": "2026-07-12", "symbols": ["AAPL"]})
    judgments = client.get("/trading-bot/judgments?market_date=2026-07-12&outcome_status=miss").json()["items"]

    assert invalid_scope.status_code == 400
    assert "No valid trading symbols supplied" in invalid_scope.json()["detail"]
    assert scored.status_code == 200
    assert scored.json()["updates"][0]["outcome_status"] == "miss"
    assert scored.json()["missed_calls"][0]["error_type"] == "trading_direct_outcome_miss"
    assert judgments[0]["outcome_summary"].startswith("return_60m=-0.031")


def test_dt_trigger_proposal_is_approval_gated_and_dispatch_records_memory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fail_shell(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        raise AssertionError("dt_trigger proposal dispatch must not run shell commands")

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fail_shell)
    client = TestClient(create_app(_config(tmp_path)))

    queued = client.post(
        "/trading-bot/dt-trigger/proposals",
        json={
            "operation": "paper-session-review",
            "target_date": "2026-07-12",
            "reason": "Review the paper session evidence before any future promotion discussion.",
            "params": {"mode": "review_only"},
        },
    )
    request_id = client.get("/approval-requests").json()["items"][0]["id"]
    dispatched = client.post(
        f"/approval-requests/{request_id}/approve",
        json={"dispatch": True, "typed_confirmation": PHRASE},
    )

    assert queued.status_code == 200
    assert queued.json()["status"] == "approval_required"
    assert queued.json()["action"] == ZADE_DT_TRIGGER_PROPOSAL_ACTION
    assert dispatched.status_code == 200
    assert dispatched.json()["dispatch_result"]["executed"] is False
    assert dispatched.json()["dispatch_result"]["memory_id"]


def test_daily_brief_can_export_to_vault_raw_folder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": (
                "report_version             : authority_health_v1\n"
                "runtime_effect             : diagnostic_only_no_live_authority\n"
                "authority_clean            : True\n"
                "[OK] authority checks are clean\n"
            ),
            "stderr": "",
        }

    def fake_query(self: TradingBotBridge, **kwargs) -> dict:
        rows = [
            {
                "id": 1,
                "timestamp": "2026-07-12T14:30:00",
                "symbol": "AAPL",
                "decision": "approved",
                "score": 74.0,
                "order_submitted": 1,
                "reason": "wealth_engine_approved: order submitted",
            }
        ]
        return {
            "columns": list(rows[0].keys()),
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
            "runtime_effect": "read_only_sqlite_no_trade_authority",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    monkeypatch.setattr(TradingBotBridge, "run_sqlite_query", fake_query)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/trading-bot/daily-brief",
        json={
            "target_date": "2026-07-12",
            "symbols": ["AAPL"],
            "snapshot_tables": ["auto_buy_candidates"],
            "include_ops_checks": ["authority-health"],
            "score_outcomes": False,
            "export_vault": True,
        },
    )

    export = response.json()["vault_export"]
    path = Path(export["path"])
    assert response.status_code == 200
    assert path == tmp_path / "hot" / "Trading Project" / "01-raw" / "zade-trading-brief-2026-07-12.md"
    assert "Zade Trading Brief - 2026-07-12" in path.read_text(encoding="utf-8")


def test_trading_bot_recent_changes_reads_git_history_and_working_tree(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    commands: list[str] = []

    def fake_run(self: TradingBotBridge, script: str, *, timeout: float | None = None) -> dict:
        commands.append(script)
        if "log" in script:
            return {
                "ok": True,
                "exit_code": 0,
                "stdout": (
                    "a1b2c3d 2026-07-15 14:02:11 -0500 Tighten auto-buy scoring threshold\n"
                    " src/trading_bot/scoring.py | 12 ++++++------\n"
                    " 1 file changed, 6 insertions(+), 6 deletions(-)\n"
                ),
                "stderr": "",
            }
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": "## main\n M config/wealth_engine.yaml\n",
            "stderr": "",
        }

    monkeypatch.setattr(TradingBotBridge, "_run_repo_shell", fake_run)
    client = TestClient(create_app(_config(tmp_path)))
    bridge = client.app.state.runtime.trading_bot

    result = bridge.recent_changes(hours=48)

    assert result["ok"] is True
    assert result["window_hours"] == 48
    assert result["runtime_effect"] == "read_only_diagnostic_no_trade_authority"
    assert "Tighten auto-buy scoring threshold" in result["commits"]["stdout"]
    assert "config/wealth_engine.yaml" in result["working_tree"]["stdout"]
    log_command = next(cmd for cmd in commands if "log" in cmd)
    assert "--since=48 hours ago" in log_command or "'--since=48 hours ago'" in log_command
    status_command = next(cmd for cmd in commands if "status" in cmd)
    assert "--short" in status_command and "diff" in status_command


def test_trading_bot_recent_changes_disabled_bridge_reports_reason(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        trading_bot=TradingBotConfig(
            enabled=False, wsl_distro="TestDistro", repo_path="/tmp/trading-bot", python="python3"
        ),
    )
    client = TestClient(create_app(config))
    bridge = client.app.state.runtime.trading_bot

    result = bridge.recent_changes()

    assert result["ok"] is False
    assert result["enabled"] is False
    assert result["reason"] == "trading-bot bridge disabled"
