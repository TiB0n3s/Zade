from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import base64
from datetime import date
from typing import Any

from .autonomy import WorkQueueService
from .config import KernelConfig
from .db import KernelDatabase, WorkItem, utc_now
from .founder import FounderService


DT_RECOMMENDATION_RUNTIME_EFFECT = "advisory_only_no_trade_authority"
ZADE_DT_RECOMMENDATION_ACTION = "external.dt_recommendation.ingest"
ZADE_DT_TRIGGER_PROPOSAL_ACTION = "external.dt_trigger.propose"
READ_ONLY_RUNTIME_EFFECT = "read_only_diagnostic_no_trade_authority"
READ_ONLY_SQLITE_RUNTIME_EFFECT = "read_only_sqlite_no_trade_authority"
DT_TRIGGER_PROPOSAL_RUNTIME_EFFECT = "proposal_only_no_trade_authority"
FULL_INTELLIGENCE_RUNTIME_EFFECT = "full_intelligence_no_broker_order_authority"

# Every "no_*_authority" / "*_mutation: False" flag in this module scopes a limit on
# the ZADE BRIDGE -- never a claim about the trading bot's own capabilities. The bot
# retains full broker/order authority in its own runtime (auto-buy / auto-sell / fill
# execution through its Alpaca+Binance gateways). Observe-only vs. live is the bot's
# own configuration state, not an absence of authority. These fields make the subject
# explicit wherever a boundary dict surfaces (audit log, /status, the chat brief).
BRIDGE_AUTHORITY_SUBJECT = "zade_trading_bot_bridge"
BOT_RUNTIME_AUTHORITY = "full_broker_order_authority_retained_by_bot"
BOT_AUTHORITY_NOTE = (
    "These flags describe what the Zade bridge cannot do, not what the bot cannot do. "
    "The bot is a live-capable automated trader with its own order+fill pipeline; "
    "'observe-only' is a config state it may or may not be in."
)


def _bridge_authority_scope() -> dict[str, Any]:
    """Subject-scoping keys added to every authority_boundary payload so a reader
    cannot mistake Zade's read-only ceiling for a property of the bot."""
    return {
        "authority_subject": BRIDGE_AUTHORITY_SUBJECT,
        "applies_to": "zade_bridge_only",
        "trading_bot_runtime_authority": BOT_RUNTIME_AUTHORITY,
        "note": BOT_AUTHORITY_NOTE,
    }

VALID_RECOMMENDATION_ACTIONS = {"buy", "sell", "hold"}
VALID_RECOMMENDATION_VERDICTS = {"recommend", "against", "abstain"}
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CLI_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.,:=@/+%-]+$")
_EVENT_TYPE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")

SAFE_OPS_CHECKS: dict[str, dict[str, Any]] = {
    "authority-health": {"date_required": False},
    "database-backups": {"date_required": False},
    "operational-readiness": {"date_required": False},
    "runtime-health": {"date_required": False},
    "d-state-processes": {"date_required": False},
    "model-governance": {"date_required": False},
    "model-promotion-evidence": {"date_required": False},
    "paper-session-evidence": {"date_required": True},
    "shadow-predictions": {"date_required": True},
    "paper-live-monitor": {"date_required": True},
    "prediction-validation": {"date_required": True},
    "live-bar-pattern-capture": {"date_required": True},
    "calibration-buckets": {"date_required": True},
    "dt-recommendations": {"date_required": True},
    "dt-recommendation-outcomes": {"date_required": True},
}

ADVISORY_DIAGNOSTIC_CHECKS = (
    "authority-health",
    "paper-live-monitor",
    "paper-session-evidence",
    "shadow-predictions",
    "dt-recommendations",
)
OUTCOME_SCORE_CHECKS = (
    "dt-recommendations",
    "dt-recommendation-outcomes",
)
DAILY_TRADING_CHECKS = (
    "authority-health",
    "runtime-health",
    "paper-live-monitor",
    "paper-session-evidence",
    "shadow-predictions",
    "dt-recommendations",
)
DAILY_TRADING_JUDGMENT_SOURCE = "zade_daily_trading_loop"
DEFAULT_TRADING_VAULT_EXPORT = ("Trading Project", "01-raw")

_DIAGNOSTIC_SYMBOL_RE = re.compile(r"\b[A-Z][A-Z0-9]{0,4}(?:\.[A-Z])?\b")
_SYMBOL_STOPWORDS = {
    "AI",
    "API",
    "AUTO",
    "BUY",
    "CLI",
    "DB",
    "DT",
    "ET",
    "EV",
    "FALSE",
    "GET",
    "JSON",
    "MFE",
    "ML",
    "NO",
    "OK",
    "POST",
    "PULL",
    "SELL",
    "SQL",
    "TRUE",
    "WARN",
    "WSL",
}
_CONCERN_MARKERS = (
    "[WARN]",
    "blocker",
    "missing",
    "stale",
    "error",
    "false",
    "disabled",
    "no recommendations",
    "no shadow prediction rows",
    "clean_for_authority_review      : False",
)
_POSITIVE_SYMBOL_MARKERS = (
    "approved",
    "recommend",
    "candidate_ready",
    "watchlist",
    "strong",
)

TRADING_SQLITE_DATABASES = {
    "trades": "trades.db",
    "trades.db": "trades.db",
}

TRADING_BOT_PYTHONPATH = "src:scripts:."

# Read-only worker that produces the compact live snapshot the chat runtime needs
# to answer PnL / trade-activity / signal questions from real rows instead of
# fabricating: today's trade activity from trades.db, account equity + intraday
# change from engine_state.db, and the latest auto-buy candidates. Stdlib only;
# every connection is mode=ro. Emits a single JSON line.
_ACTIVITY_SNAPSHOT_SCRIPT = r"""
import sqlite3, json, datetime as dt

def _date(ns):
    return dt.datetime.fromtimestamp(ns / 1e9, dt.timezone.utc).date()

out = {"trades": {}, "equity": {}, "signals": [], "errors": []}

try:
    t = sqlite3.connect("file:trades.db?mode=ro", uri=True)
    t.row_factory = sqlite3.Row
    t.execute("PRAGMA query_only = ON")
    r = t.execute(
        "SELECT COUNT(*) n, SUM(action='buy') buys, SUM(action='sell') sells, "
        "COUNT(DISTINCT symbol) syms FROM trades WHERE date(timestamp)=date('now')"
    ).fetchone()
    out["trades"] = {
        "today_total": r["n"] or 0,
        "buys": r["buys"] or 0,
        "sells": r["sells"] or 0,
        "symbols": r["syms"] or 0,
    }
    out["trades"]["recent_fills"] = [
        dict(x) for x in t.execute(
            "SELECT timestamp,symbol,action,qty,fill_price,order_status "
            "FROM trades ORDER BY timestamp DESC LIMIT 10"
        )
    ]
    try:
        out["signals"] = [
            dict(x) for x in t.execute(
                "SELECT timestamp,symbol,decision,score,reason "
                "FROM auto_buy_candidates ORDER BY timestamp DESC LIMIT 5"
            )
        ]
    except Exception as exc:
        out["errors"].append("signals:%s" % exc)
except Exception as exc:
    out["errors"].append("trades:%s" % exc)

try:
    e = sqlite3.connect("file:engine_state.db?mode=ro", uri=True)
    e.execute("PRAGMA query_only = ON")
    rows = e.execute("SELECT ts_ns, equity FROM equity_samples ORDER BY ts_ns DESC LIMIT 5000").fetchall()
    if rows:
        latest_ns, latest_eq = rows[0]
        latest_day = _date(latest_ns)
        today = [(ns, eq) for ns, eq in rows if _date(ns) == latest_day]
        prior = [(ns, eq) for ns, eq in rows if _date(ns) < latest_day]
        first_today = today[-1][1] if today else latest_eq
        prior_close = prior[0][1] if prior else first_today
        out["equity"] = {
            "latest_equity": round(latest_eq, 2),
            "session_date": str(latest_day),
            "intraday_change": round(latest_eq - first_today, 2),
            "change_vs_prior_close": round(latest_eq - prior_close, 2),
            "samples_today": len(today),
        }
except Exception as exc:
    out["errors"].append("equity:%s" % exc)

print(json.dumps(out, default=str))
""".strip()

TRADING_TRAINING_COMMANDS: dict[str, dict[str, Any]] = {
    "supervised-predictions": {
        "script": "scripts/train_supervised_predictions.py",
        "description": "Train/evaluate the observe-only supervised prediction scaffold.",
        "date_flag": None,
        "date_required": False,
        "symbol_flag": "--symbol",
        "multi_symbol": False,
    },
    "regime-model": {
        "script": "scripts/train_regime_model.py",
        "description": "Train the optional HMM regime model from feature snapshots.",
        "date_flag": None,
        "date_required": False,
        "symbol_flag": None,
        "multi_symbol": False,
    },
    "pipeline-retrain": {
        "script": "pipeline/retrain.py",
        "description": "Run the automated observe-only ML retraining trigger.",
        "date_flag": "--date",
        "date_required": False,
        "symbol_flag": None,
        "multi_symbol": False,
    },
    "symbol-universe": {
        "script": "pipeline/symbol_universe_retrain.py",
        "description": "Retrain when the approved symbol universe changes.",
        "date_flag": "--date",
        "date_required": True,
        "symbol_flag": None,
        "multi_symbol": False,
    },
    "historical-bar-model": {
        "script": "pipeline/train_historical_bar_model.py",
        "description": "Train observe-only models directly from historical bar pattern features.",
        "date_flag": "--end-date",
        "date_required": False,
        "symbol_flag": "--symbol",
        "multi_symbol": False,
    },
}

TRADING_EVENT_TABLE = {
    "columns": [
        "id",
        "timestamp",
        "event_type",
        "symbol",
        "action",
        "decision",
        "severity",
        "reason",
        "source",
        "payload_json",
    ],
    "order_by": [("timestamp", "DESC"), ("id", "DESC")],
    "symbol_column": "symbol",
    "event_type_column": "event_type",
    "since_column": "timestamp",
}

TRADING_SIGNAL_TABLES: dict[str, dict[str, Any]] = {
    "webhook_events": {
        "columns": [
            "id",
            "dedupe_key",
            "received_at",
            "symbol",
            "action",
            "signal_price",
            "source",
            "status",
            "queued_at",
            "started_at",
            "finished_at",
            "order_id",
            "client_order_id",
            "failure_reason",
        ],
        "order_by": [("received_at", "DESC"), ("id", "DESC")],
        "symbol_column": "symbol",
    },
    "auto_buy_candidates": {
        "columns": [
            "id",
            "timestamp",
            "symbol",
            "signal_source",
            "decision",
            "score",
            "reason",
            "setup_label",
            "setup_score",
            "annotation_prediction_score",
            "order_submitted",
            "hard_block_reason",
        ],
        "order_by": [("timestamp", "DESC"), ("id", "DESC")],
        "symbol_column": "symbol",
    },
    "auto_buy_decision_snapshots": {
        "columns": [
            "id",
            "created_at",
            "candidate_timestamp",
            "symbol",
            "signal_source",
            "decision",
            "score",
            "reason",
            "hard_block_reason",
            "order_submitted",
            "order_status",
            "runtime_effect",
            "execution_status",
        ],
        "order_by": [("created_at", "DESC"), ("id", "DESC")],
        "symbol_column": "symbol",
    },
    "auto_sell_candidates": {
        "columns": [
            "id",
            "timestamp",
            "symbol",
            "qty",
            "action",
            "severity",
            "reason",
            "sell_pressure_score",
            "sell_pressure_recommendation",
            "auto_sell_enabled",
            "order_submitted",
            "order_id",
        ],
        "order_by": [("timestamp", "DESC"), ("id", "DESC")],
        "symbol_column": "symbol",
    },
}

TRADING_MARKET_CONTEXT_TABLES: dict[str, dict[str, Any]] = {
    "daily_symbol_context": {
        "columns": [
            "id",
            "market_date",
            "symbol",
            "source",
            "macro_sentiment",
            "macro_regime",
            "risk_multiplier",
            "block_new_buys",
            "bias",
            "confidence",
            "fundamental_score",
            "risk_level",
            "entry_quality",
            "avoid_type",
            "reason",
            "daily_pct",
            "intraday_pct",
            "momentum_30m_pct",
            "last_price",
            "catalyst_score",
            "relative_strength_score",
            "sector_alignment",
            "raw_json",
            "created_at",
            "updated_at",
        ],
        "date_column": "market_date",
        "symbol_column": "symbol",
        "order_by": [("market_date", "DESC"), ("updated_at", "DESC"), ("id", "DESC")],
    },
}

_MARKET_CONTEXT_FILE_SCRIPT = """
import json
from pathlib import Path

path = Path("market_context.json")
payload = {"exists": path.exists(), "path": str(path), "data": None, "error": ""}
if path.exists():
    try:
        payload["data"] = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        payload["error"] = f"{exc.__class__.__name__}: {exc}"
print(json.dumps(payload, sort_keys=True, default=str))
""".strip()

TRADING_EVIDENCE_TABLES: dict[str, dict[str, Any]] = {
    "auto_buy_candidates": {
        "date_column": "timestamp",
        "columns": [
            "id",
            "timestamp",
            "symbol",
            "decision",
            "score",
            "reason",
            "setup_label",
            "setup_score",
            "prediction_score",
            "order_submitted",
            "hard_block_reason",
        ],
        "order_by": "timestamp DESC",
    },
    "trades": {
        "date_column": "timestamp",
        "columns": [
            "id",
            "timestamp",
            "symbol",
            "action",
            "approved",
            "order_status",
            "confidence",
            "prediction_score",
            "setup_label",
            "buy_opportunity_score",
            "rejection_reason",
        ],
        "order_by": "timestamp DESC",
    },
    "shadow_predictions": {
        "date_column": "market_date",
        "columns": [
            "id",
            "market_date",
            "symbol",
            "prediction_time",
            "model_id",
            "prediction_score",
            "raw_prediction_score",
            "runtime_effect",
        ],
        "order_by": "prediction_time DESC, id DESC",
    },
    "decision_snapshots": {
        "date_column": "decision_time",
        "columns": [
            "id",
            "decision_time",
            "symbol",
            "action",
            "final_decision",
            "approved",
            "rejection_reason",
            "confidence",
            "prediction_score",
            "setup_label",
            "buy_opportunity_score",
        ],
        "order_by": "decision_time DESC",
    },
    "rejected_signal_outcomes": {
        "date_column": "timestamp",
        "columns": [
            "id",
            "timestamp",
            "symbol",
            "action",
            "rejection_reason",
            "return_5m",
            "return_15m",
            "return_30m",
            "return_60m",
            "return_eod",
            "max_favorable_60m",
            "max_adverse_60m",
            "label_status",
        ],
        "order_by": "timestamp DESC",
    },
    "dt_recommendations": {
        "date_column": "market_date",
        "columns": [
            "id",
            "created_at",
            "market_date",
            "symbol",
            "action",
            "verdict",
            "conviction",
            "reason",
            "agent_version",
            "runtime_effect",
        ],
        "order_by": "created_at DESC",
    },
}

_SQLITE_WORKER_SCRIPT = r"""
import base64
import json
import pathlib
import sqlite3
import sys
import time

payload = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
db_path = pathlib.Path(payload["database"])
if not db_path.is_absolute():
    db_path = pathlib.Path.cwd() / db_path
limit = int(payload["limit"])
deadline = time.monotonic() + float(payload["timeout_seconds"])

try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")

    def progress():
        return 1 if time.monotonic() > deadline else 0

    con.set_progress_handler(progress, 1000)
    cur = con.execute(payload["sql"], payload["params"])
    columns = [item[0] for item in cur.description] if cur.description else []
    rows = []
    truncated = False
    if columns:
        fetched = cur.fetchmany(limit + 1)
        truncated = len(fetched) > limit
        rows = [dict(row) for row in fetched[:limit]]
    print(json.dumps({
        "ok": True,
        "database": str(db_path),
        "query_only": bool(con.execute("PRAGMA query_only").fetchone()[0]),
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }, default=str))
except Exception as exc:
    print(json.dumps({
        "ok": False,
        "database": str(db_path),
        "error": str(exc),
        "error_type": exc.__class__.__name__,
    }, default=str))
    raise SystemExit(2)
"""


class TradingBotBridge:
    """Authority-safe bridge into the existing trading-bot advisory seams.

    This service deliberately avoids broker, sizing, execution, gate, and
    runtime decision paths. Writes go only through the bot-owned
    dt_recommendation_ingest.py CLI, which appends advisory bookkeeping rows
    that the bot architecture tests prevent runtime code from reading.
    """

    def __init__(self, *, config: KernelConfig, db: KernelDatabase, founder: FounderService | None = None):
        self.config = config
        self.db = db
        self.founder = founder

    def status(self) -> dict[str, Any]:
        cfg = self.config.trading_bot
        if not cfg.enabled:
            return {
                "ok": False,
                "enabled": False,
                "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
                "reason": "trading-bot bridge disabled",
            }

        repo_probe = self._run_repo_shell("pwd", timeout=15)
        git_probe = self._run_repo_shell(
            f"{_shell_join(['git', '-c', f'safe.directory={cfg.repo_path}', 'status', '--short', '--branch'])} && "
            f"{_shell_join(['git', '-c', f'safe.directory={cfg.repo_path}', 'log', '-1', '--oneline'])}",
            timeout=30,
        )
        lane_probe = self._run_repo_shell(
            "test -f ops/dt_recommendation_lane_spec.md && "
            "test -f scripts/dt_recommendation_ingest.py && "
            "test -f src/trading_bot/persistence/repositories/dt_recommendation_repo.py && "
            "printf advisory_lane_present",
            timeout=30,
        )
        ok = repo_probe["ok"] and lane_probe["ok"]
        return {
            "ok": ok,
            "enabled": True,
            "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
            "wsl_distro": cfg.wsl_distro,
            "repo_path": cfg.repo_path,
            "repo_reachable": repo_probe["ok"],
            "advisory_lane_present": lane_probe["ok"],
            "git": _compact_probe(git_probe, limit=4000),
            "safe_ops_checks": self.safe_ops_checks(),
            "intelligence_access": self.intelligence_access(),
            "authority_boundary": {
                "writes": "allowlisted training artifacts plus approval-gated append-only dt_recommendations ingest",
                "runtime_read_path": "intelligence context only; advisory rows are not broker/order runtime inputs",
                "broker_order_sizing_gate_mutation": False,
                **_bridge_authority_scope(),
            },
            "deep_thought_replacement": self.deep_thought_replacement_map(),
        }

    def activity_snapshot(self, *, limit_output_chars: int = 6000) -> dict[str, Any]:
        """Compact, read-only live trading data for chat context: today's trade
        activity, account equity + intraday change, and the latest auto-buy
        candidates. This is what lets Zade answer PnL / trade / signal questions
        from real rows instead of fabricating. Reads trades.db and
        engine_state.db read-only; never touches broker/order/runtime paths."""
        cfg = self.config.trading_bot
        if not cfg.enabled:
            return {
                "ok": False,
                "enabled": False,
                "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
                "reason": "trading-bot bridge disabled",
                "trades": {},
                "equity": {},
                "signals": [],
                "errors": ["trading-bot bridge disabled"],
            }
        result = self._run_repo_shell(
            _shell_join([cfg.python, "-c", _ACTIVITY_SNAPSHOT_SCRIPT]),
            timeout=30,
        )
        parsed = _parse_json_value(result["stdout"])
        data = parsed if isinstance(parsed, dict) else {}
        errors = list(data.get("errors") or [])
        if not result["ok"] and not errors:
            errors.append(_limit(result["stderr"], 300))
        ok = bool(result["ok"] and data and not data.get("errors"))
        self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.activity.snapshot",
            target="today",
            permission_tier="L0_READ",
            status="ok" if ok else "error",
            details={
                "today_total": (data.get("trades") or {}).get("today_total"),
                "latest_equity": (data.get("equity") or {}).get("latest_equity"),
                "exit_code": result["exit_code"],
                "errors": errors[:3],
                "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
            },
        )
        return {
            "ok": ok,
            "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
            "authority_boundary": _sqlite_authority_boundary(),
            "trades": data.get("trades") or {},
            "equity": data.get("equity") or {},
            "signals": data.get("signals") or [],
            "errors": errors,
            "probe": _compact_probe(result, limit=limit_output_chars),
        }

    def safe_ops_checks(self) -> list[dict[str, Any]]:
        return [
            {
                "command": command,
                "date_required": bool(spec["date_required"]),
                "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
            }
            for command, spec in sorted(SAFE_OPS_CHECKS.items())
        ]

    def intelligence_access(self) -> dict[str, Any]:
        enabled = bool(self.config.trading_bot.enabled)
        return {
            "ok": enabled,
            "enabled": enabled,
            "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
            "capabilities": {
                "training": {
                    "enabled": enabled,
                    "commands": sorted(TRADING_TRAINING_COMMANDS),
                    "details": {
                        command: {
                            "script": spec["script"],
                            "description": spec["description"],
                            "date_required": bool(spec.get("date_required")),
                            "date_flag": spec.get("date_flag"),
                            "symbol_flag": spec.get("symbol_flag"),
                            "multi_symbol": bool(spec.get("multi_symbol")),
                        }
                        for command, spec in sorted(TRADING_TRAINING_COMMANDS.items())
                    },
                },
                "advisory": {
                    "enabled": enabled,
                    "routes": [
                        "POST /trading-bot/advisory/generate",
                        "POST /trading-bot/recommendations",
                        "POST /trading-bot/daily-brief",
                    ],
                },
                "events": {
                    "read": enabled,
                    "route": "GET /trading-bot/events/recent",
                    "table": "bot_events",
                },
                "market_context": {
                    "read": enabled,
                    "route": "GET /trading-bot/market-context",
                    "tables": sorted(TRADING_MARKET_CONTEXT_TABLES),
                    "file": "market_context.json",
                },
                "signals": {
                    "watch": enabled,
                    "route": "GET /trading-bot/signals/recent",
                    "tables": sorted(TRADING_SIGNAL_TABLES),
                },
                "sqlite": {
                    "read": enabled,
                    "routes": [
                        "GET /trading-bot/sqlite/schema",
                        "POST /trading-bot/sqlite/query",
                        "POST /trading-bot/evidence/snapshot",
                    ],
                },
            },
            "authority_boundary": _full_intelligence_authority_boundary(),
        }

    def run_training(
        self,
        *,
        command: str,
        target_date: str | None = None,
        symbols: list[str] | None = None,
        extra_args: list[str] | None = None,
        timeout_seconds: float = 300.0,
        limit_output_chars: int = 12000,
    ) -> dict[str, Any]:
        command_key = str(command or "").strip()
        spec = TRADING_TRAINING_COMMANDS.get(command_key)
        if not spec:
            raise ValueError(f"Unsupported trading-bot training command: {command}")

        args = [
            "env",
            f"PYTHONPATH={TRADING_BOT_PYTHONPATH}",
            self.config.trading_bot.python,
            spec["script"],
        ]
        if target_date:
            _validate_date(target_date)
            date_flag = spec.get("date_flag")
            if not date_flag:
                raise ValueError(f"{command_key} does not accept target_date through the Zade bridge.")
            args.extend([str(date_flag), target_date])
        elif spec.get("date_required"):
            raise ValueError(f"{command_key} requires target_date.")

        symbol_targets = _normalize_requested_symbols(symbols)
        if symbol_targets:
            symbol_flag = spec.get("symbol_flag")
            if not symbol_flag:
                raise ValueError(f"{command_key} does not accept symbols through the Zade bridge.")
            if len(symbol_targets) > 1 and not spec.get("multi_symbol"):
                raise ValueError(f"{command_key} accepts only one symbol per run.")
            for symbol in symbol_targets:
                args.extend([str(symbol_flag), symbol])

        safe_extra_args = _validate_cli_extra_args(extra_args)
        args.extend(safe_extra_args)
        timeout_seconds = max(1.0, min(3600.0, float(timeout_seconds)))
        limit_output_chars = max(100, min(50000, int(limit_output_chars)))

        result = self._run_repo_shell(_shell_join(args), timeout=timeout_seconds)
        parsed = _parse_json_value(result["stdout"])
        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.training.run",
            target=command_key,
            permission_tier="L2_FILE_WRITE",
            status="ok" if result["ok"] else "error",
            details={
                "command": command_key,
                "script": spec["script"],
                "target_date": target_date,
                "symbols": symbol_targets,
                "extra_args": safe_extra_args,
                "exit_code": result["exit_code"],
                "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
                "broker_order_sizing_gate_mutation": False,
            },
        )
        return {
            "command": command_key,
            "script": spec["script"],
            "target_date": target_date,
            "symbols": symbol_targets,
            "extra_args": safe_extra_args,
            "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
            "authority_boundary": _full_intelligence_authority_boundary(),
            "ok": result["ok"],
            "exit_code": result["exit_code"],
            "stdout": _limit(result["stdout"], limit_output_chars),
            "stderr": _limit(result["stderr"], limit_output_chars),
            "parsed": parsed,
            "audit_id": audit_id,
        }

    def recent_events(
        self,
        *,
        limit: int = 50,
        event_type: str | None = None,
        symbol: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(500, int(limit)))
        event_type_filter = _validate_event_type(event_type)
        symbol_filter = _normalize_single_symbol(symbol)
        since_filter = _validate_cli_optional_value(since, "since") if since else None
        args = [
            "env",
            f"PYTHONPATH={TRADING_BOT_PYTHONPATH}",
            self.config.trading_bot.python,
            "scripts/bot_events.py",
            "--json",
            "--limit",
            str(limit),
        ]
        if event_type_filter:
            args.extend(["--event-type", event_type_filter])
        if symbol_filter:
            args.extend(["--symbol", symbol_filter])
        if since_filter:
            args.extend(["--since", since_filter])

        result = self._run_repo_shell(_shell_join(args), timeout=30)
        parsed = _parse_json_value(result["stdout"])
        items = _json_rows(parsed)
        source = "scripts/bot_events.py"
        fallback: dict[str, Any] | None = None
        if not result["ok"] or parsed is None:
            source = "sqlite:bot_events"
            fallback = self._query_table_snapshot(
                table="bot_events",
                spec=TRADING_EVENT_TABLE,
                limit=limit,
                symbol=symbol_filter,
                event_type=event_type_filter,
                since=since_filter,
            )
            items = list(fallback.get("rows") or [])

        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.events.recent",
            target=event_type_filter or "all",
            permission_tier="L0_READ",
            status="ok" if result["ok"] or (fallback and not fallback.get("error")) else "error",
            details={
                "limit": limit,
                "event_type": event_type_filter,
                "symbol": symbol_filter,
                "since": since_filter,
                "source": source,
                "row_count": len(items),
                "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
            },
        )
        return {
            "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
            "authority_boundary": _full_intelligence_authority_boundary(),
            "source": source,
            "filters": {
                "limit": limit,
                "event_type": event_type_filter,
                "symbol": symbol_filter,
                "since": since_filter,
            },
            "items": items,
            "row_count": len(items),
            "script_probe": _compact_probe(result, limit=4000),
            "fallback": fallback,
            "audit_id": audit_id,
        }

    def recent_signals(self, *, limit: int = 50, symbol: str | None = None) -> dict[str, Any]:
        limit = max(1, min(500, int(limit)))
        symbol_filter = _normalize_single_symbol(symbol)
        tables: dict[str, Any] = {}
        for table_name, spec in TRADING_SIGNAL_TABLES.items():
            tables[table_name] = self._query_table_snapshot(
                table=table_name,
                spec=spec,
                limit=limit,
                symbol=symbol_filter,
            )
        total_rows = sum(len(result.get("rows") or []) for result in tables.values())
        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.signals.recent",
            target=symbol_filter or "all",
            permission_tier="L0_READ",
            status="ok",
            details={
                "limit": limit,
                "symbol": symbol_filter,
                "tables": sorted(tables),
                "total_rows": total_rows,
                "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
            },
        )
        return {
            "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
            "authority_boundary": _full_intelligence_authority_boundary(),
            "filters": {"limit": limit, "symbol": symbol_filter},
            "summary": {"total_rows": total_rows, "tables": sorted(tables)},
            "tables": tables,
            "audit_id": audit_id,
        }

    def market_context(
        self,
        *,
        target_date: str | None = None,
        symbol: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        if target_date:
            _validate_date(target_date)
        limit = max(1, min(200, int(limit)))
        symbol_filter = _normalize_single_symbol(symbol)
        file_result = self._read_market_context_file()
        tables: dict[str, Any] = {}
        for table_name, spec in TRADING_MARKET_CONTEXT_TABLES.items():
            tables[table_name] = self._query_table_snapshot(
                table=table_name,
                spec=spec,
                limit=limit,
                target_date=target_date,
                symbol=symbol_filter,
            )
        total_rows = sum(len(result.get("rows") or []) for result in tables.values())
        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.market_context.read",
            target=target_date or "latest",
            permission_tier="L0_READ",
            status="ok" if file_result.get("ok") or total_rows else "error",
            details={
                "target_date": target_date,
                "symbol": symbol_filter,
                "limit": limit,
                "file_exists": file_result.get("exists"),
                "total_rows": total_rows,
                "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
            },
        )
        return {
            "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
            "authority_boundary": _full_intelligence_authority_boundary(),
            "filters": {"target_date": target_date, "symbol": symbol_filter, "limit": limit},
            "market_context_file": file_result,
            "summary": {"total_rows": total_rows, "tables": sorted(tables)},
            "tables": tables,
            "audit_id": audit_id,
        }

    def sqlite_schema(
        self,
        *,
        database: str = "trades.db",
        table: str | None = None,
        include_counts: bool = False,
    ) -> dict[str, Any]:
        database_path = _sqlite_database_path(database)
        table_filter = _validate_sql_identifier(table, "table") if table else None
        table_rows = self.run_sqlite_query(
            sql="SELECT name FROM sqlite_master WHERE type = ? ORDER BY name",
            params=["table"],
            limit=500,
            database=database_path,
            timeout_seconds=5.0,
        )["rows"]
        table_names = [row["name"] for row in table_rows]
        if table_filter:
            table_names = [name for name in table_names if name == table_filter]
        items: list[dict[str, Any]] = []
        for table_name in table_names:
            columns = self.run_sqlite_query(
                sql=f"PRAGMA table_info({_quote_identifier(table_name)})",
                limit=500,
                database=database_path,
                timeout_seconds=5.0,
            )["rows"]
            item: dict[str, Any] = {
                "table": table_name,
                "columns": [
                    {
                        "name": row.get("name"),
                        "type": row.get("type"),
                        "notnull": bool(row.get("notnull")),
                        "primary_key": bool(row.get("pk")),
                        "default": row.get("dflt_value"),
                    }
                    for row in columns
                ],
            }
            if include_counts:
                count_result = self.run_sqlite_query(
                    sql=f"SELECT COUNT(*) AS row_count FROM {_quote_identifier(table_name)}",
                    limit=1,
                    database=database_path,
                    timeout_seconds=10.0,
                )
                item["row_count"] = count_result["rows"][0]["row_count"] if count_result["rows"] else None
            items.append(item)
        return {
            "database": database_path,
            "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
            "table_filter": table_filter,
            "include_counts": include_counts,
            "tables": items,
            "authority_boundary": _sqlite_authority_boundary(),
        }

    def run_sqlite_query(
        self,
        *,
        sql: str,
        params: list[Any] | dict[str, Any] | None = None,
        limit: int = 100,
        timeout_seconds: float = 5.0,
        database: str = "trades.db",
    ) -> dict[str, Any]:
        database_path = _sqlite_database_path(database)
        limit = max(1, min(1000, int(limit)))
        timeout_seconds = max(0.1, min(30.0, float(timeout_seconds)))
        try:
            normalized_sql = _validate_sqlite_read_sql(sql)
            safe_params = _validate_sqlite_params(params)
        except ValueError as exc:
            self.db.audit(
                actor="trading_bot.bridge",
                action="trading_bot.sqlite.blocked",
                target=_limit(sql, 240),
                permission_tier="L0_READ",
                status="blocked",
                details={
                    "reason": str(exc),
                    "database": database_path,
                    "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
                },
            )
            raise

        worker_payload = {
            "database": database_path,
            "sql": normalized_sql,
            "params": safe_params,
            "limit": limit,
            "timeout_seconds": timeout_seconds,
        }
        encoded = base64.b64encode(json.dumps(worker_payload, separators=(",", ":"), default=str).encode("utf-8")).decode("ascii")
        result = self._run_repo_shell(_shell_join([self.config.trading_bot.python, "-c", _SQLITE_WORKER_SCRIPT, encoded]), timeout=timeout_seconds + 5)
        parsed = _parse_json(result["stdout"]) or {}
        ok = bool(result["ok"] and parsed.get("ok"))
        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.sqlite.read",
            target=_limit(normalized_sql, 240),
            permission_tier="L0_READ",
            status="ok" if ok else "error",
            details={
                "database": database_path,
                "limit": limit,
                "timeout_seconds": timeout_seconds,
                "exit_code": result["exit_code"],
                "row_count": parsed.get("row_count"),
                "truncated": parsed.get("truncated"),
                "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
                "error": parsed.get("error") or result["stderr"][:500],
            },
        )
        if not ok:
            raise ValueError(
                "trading-bot SQLite read failed: "
                f"exit={result['exit_code']} error={parsed.get('error') or result['stderr'][:500]}"
            )
        return {
            "database": database_path,
            "sql": normalized_sql,
            "params": safe_params,
            "limit": limit,
            "timeout_seconds": timeout_seconds,
            "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
            "query_only": bool(parsed.get("query_only")),
            "columns": list(parsed.get("columns") or []),
            "rows": list(parsed.get("rows") or []),
            "row_count": int(parsed.get("row_count") or 0),
            "truncated": bool(parsed.get("truncated")),
            "audit_id": audit_id,
            "authority_boundary": _sqlite_authority_boundary(),
        }

    def evidence_snapshot(
        self,
        *,
        target_date: str,
        symbols: list[str] | None = None,
        tables: list[str] | None = None,
        limit_per_table: int = 25,
        store_evidence: bool = True,
    ) -> dict[str, Any]:
        _validate_date(target_date)
        symbol_targets = _normalize_requested_symbols(symbols)
        table_targets = _validate_snapshot_tables(tables)
        limit_per_table = max(1, min(200, int(limit_per_table)))
        table_results: dict[str, Any] = {}
        for table_name in table_targets:
            query = _snapshot_query(
                table=table_name,
                target_date=target_date,
                symbols=symbol_targets,
                limit=limit_per_table,
            )
            try:
                table_results[table_name] = self.run_sqlite_query(
                    sql=query["sql"],
                    params=query["params"],
                    limit=limit_per_table,
                    timeout_seconds=10.0,
                    database="trades.db",
                )
            except ValueError as exc:
                table_results[table_name] = {
                    "ok": False,
                    "table": table_name,
                    "error": str(exc),
                    "rows": [],
                    "row_count": 0,
                    "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
                }

        summary = _summarize_evidence_snapshot(
            target_date=target_date,
            symbols=symbol_targets,
            tables=table_results,
        )
        evidence_record: dict[str, Any] = {}
        if store_evidence:
            evidence_record = self._record_snapshot_evidence(
                target_date=target_date,
                symbols=symbol_targets,
                table_results=table_results,
                summary=summary,
            )
        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.evidence.snapshot",
            target=target_date,
            permission_tier="L1_MEMORY_WRITE" if store_evidence else "L0_READ",
            status="ok",
            details={
                "symbols": symbol_targets,
                "tables": table_targets,
                "total_rows": summary["total_rows"],
                "evidence_id": evidence_record.get("evidence_id"),
                "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
            },
        )
        return {
            "target_date": target_date,
            "symbols": symbol_targets,
            "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
            "authority_boundary": _sqlite_authority_boundary(),
            "summary": summary,
            "tables": table_results,
            "evidence": evidence_record,
            "audit_id": audit_id,
        }

    def run_ops_check(
        self,
        *,
        command: str,
        target_date: str | None = None,
        limit_output_chars: int = 12000,
    ) -> dict[str, Any]:
        command = command.strip()
        spec = SAFE_OPS_CHECKS.get(command)
        if not spec:
            raise ValueError(f"Unsupported trading-bot ops check: {command}")
        args = [self.config.trading_bot.python, "ops_check.py", command]
        if spec["date_required"]:
            if not target_date:
                raise ValueError(f"{command} requires target_date.")
            _validate_date(target_date)
            args.append(target_date)
        elif target_date:
            raise ValueError(f"{command} does not accept target_date through the Zade bridge.")

        result = self._run_repo_shell(_shell_join(args))
        self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.ops_check.read",
            target=command,
            permission_tier="L0_READ",
            status="ok" if result["ok"] else "error",
            details={
                "command": command,
                "target_date": target_date,
                "exit_code": result["exit_code"],
                "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
            },
        )
        return {
            "command": command,
            "target_date": target_date,
            "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
            "ok": result["ok"],
            "exit_code": result["exit_code"],
            "stdout": _limit(result["stdout"], limit_output_chars),
            "stderr": _limit(result["stderr"], limit_output_chars),
        }

    def queue_advisory_recommendation(
        self,
        *,
        work_queue: WorkQueueService,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        recommendation = self.normalize_recommendation(payload)
        result = work_queue.enqueue(
            kind="trading_bot_advisory",
            title=(
                f"Send {recommendation['symbol']} {recommendation['action']} "
                f"{recommendation['verdict']} advisory to trading bot"
            ),
            detail=recommendation["reason"],
            action=ZADE_DT_RECOMMENDATION_ACTION,
            target="dt_recommendations:advisory_only",
            permission_tier="L3_EXTERNAL_ACTION",
            priority=int(payload.get("priority", 90)),
            source="trading_bot.bridge",
            metadata={
                "recommendation": recommendation,
                "runtime_effect": DT_RECOMMENDATION_RUNTIME_EFFECT,
                "evidence": list(payload.get("evidence") or []),
                "risks": list(payload.get("risks") or _default_recommendation_risks()),
                "authority_boundary": {
                    "bot_runtime_reads_row": False,
                    "can_approve_block_size_or_order": False,
                    "bot_final_validation": "scripts/dt_recommendation_ingest.py",
                },
            },
            unique_key=f"zade:dt_recommendation:{recommendation['idempotency_key']}",
        )
        return {
            **result.as_dict(),
            "recommendation": recommendation,
            "runtime_effect": DT_RECOMMENDATION_RUNTIME_EFFECT,
        }

    def generate_advisory_recommendations(
        self,
        *,
        work_queue: WorkQueueService,
        target_date: str,
        symbols: list[str] | None = None,
        queue: bool = True,
        max_recommendations: int = 10,
        include_ops_checks: list[str] | None = None,
        limit_output_chars: int = 12000,
        priority: int = 90,
        use_sqlite_snapshot: bool = True,
        snapshot_tables: list[str] | None = None,
        snapshot_limit_per_table: int = 25,
    ) -> dict[str, Any]:
        _validate_date(target_date)
        max_recommendations = max(0, min(50, int(max_recommendations)))
        checks = self._advisory_checks(include_ops_checks)
        diagnostics = [
            self.run_ops_check(
                command=command,
                target_date=target_date if SAFE_OPS_CHECKS[command]["date_required"] else None,
                limit_output_chars=limit_output_chars,
            )
            for command in checks
        ]
        summary = _summarize_diagnostics(diagnostics)
        requested_symbols = _normalize_requested_symbols(symbols)
        sqlite_snapshot: dict[str, Any] = {}
        if use_sqlite_snapshot:
            sqlite_snapshot = self.evidence_snapshot(
                target_date=target_date,
                symbols=requested_symbols,
                tables=snapshot_tables,
                limit_per_table=snapshot_limit_per_table,
                store_evidence=False,
            )
            summary = _merge_sqlite_snapshot(summary, sqlite_snapshot)
        evidence_record = self._record_diagnostic_evidence(
            evidence_type="trading_bot_advisory_generation",
            target_date=target_date,
            diagnostics=diagnostics,
            summary=summary,
            claim_supported=(
                "Zade generated advisory trading-bot recommendations from read-only bot diagnostics and SQLite evidence."
                if requested_symbols or summary["symbols"]
                else "Zade collected trading-bot diagnostics and SQLite evidence but found no safe symbol-specific advisory target."
            ),
            claim_contradicted=(
                "Trading-bot diagnostics contain blockers or warnings that limit advisory confidence."
                if summary["concerns"]
                else ""
            ),
        )
        symbol_targets = _normalize_symbols(requested_symbols or summary["symbols"])
        candidates = self._build_advisory_candidates(
            target_date=target_date,
            symbols=symbol_targets[:max_recommendations],
            summary=summary,
            sqlite_snapshot=sqlite_snapshot,
            evidence_record=evidence_record,
            priority=priority,
        )
        queued: list[dict[str, Any]] = []
        prepared: list[dict[str, Any]] = []
        for candidate in candidates:
            normalized = self.normalize_recommendation(candidate)
            prepared.append(normalized | {"evidence": candidate.get("evidence", [])})
            if queue:
                queued.append(
                    self.queue_advisory_recommendation(
                        work_queue=work_queue,
                        payload=candidate,
                    )
                )

        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.advisory.generate",
            target=target_date,
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={
                "commands": checks,
                "symbol_targets": symbol_targets,
                "sqlite_snapshot": bool(sqlite_snapshot),
                "prepared_count": len(prepared),
                "queued_count": len(queued),
                "evidence_id": evidence_record.get("evidence_id"),
                "runtime_effect": DT_RECOMMENDATION_RUNTIME_EFFECT,
            },
        )
        return {
            "target_date": target_date,
            "runtime_effect": DT_RECOMMENDATION_RUNTIME_EFFECT,
            "authority_boundary": _authority_boundary(),
            "diagnostics": diagnostics,
            "summary": summary,
            "sqlite_snapshot": _compact_snapshot(sqlite_snapshot),
            "evidence": evidence_record,
            "recommendations": prepared,
            "queued": queued,
            "skipped": [] if symbol_targets else [_no_symbol_skip(summary)],
            "queue_requested": queue,
            "audit_id": audit_id,
        }

    def score_advisory_outcomes(
        self,
        *,
        target_date: str,
        store_evidence: bool = True,
        limit_output_chars: int = 12000,
    ) -> dict[str, Any]:
        _validate_date(target_date)
        diagnostics = [
            self.run_ops_check(
                command=command,
                target_date=target_date,
                limit_output_chars=limit_output_chars,
            )
            for command in OUTCOME_SCORE_CHECKS
        ]
        scorecard = _parse_outcome_scorecard(diagnostics)
        evidence_record: dict[str, Any] = {}
        if store_evidence:
            evidence_record = self._record_diagnostic_evidence(
                evidence_type="trading_bot_advisory_outcome_score",
                target_date=target_date,
                diagnostics=diagnostics,
                summary=_summarize_diagnostics(diagnostics) | {"scorecard": scorecard},
                claim_supported=(
                    f"Zade scored {scorecard['recommendations']} advisory recommendations against bot outcomes."
                    if scorecard["recommendations"]
                    else "Zade ran advisory outcome scoring; no recommendations were available to score."
                ),
                claim_contradicted=(
                    "The advisory recommendation lane has no scored rows for this date."
                    if not scorecard["recommendations"]
                    else ""
                ),
            )
        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.advisory.score",
            target=target_date,
            permission_tier="L1_MEMORY_WRITE" if store_evidence else "L0_READ",
            status="ok",
            details={
                "scorecard": scorecard,
                "evidence_id": evidence_record.get("evidence_id"),
                "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
            },
        )
        return {
            "target_date": target_date,
            "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
            "authority_boundary": _authority_boundary(),
            "scorecard": scorecard,
            "diagnostics": diagnostics,
            "evidence": evidence_record,
            "audit_id": audit_id,
        }

    def daily_trading_brief(
        self,
        *,
        target_date: str,
        symbols: list[str] | None = None,
        snapshot_tables: list[str] | None = None,
        limit_per_table: int = 25,
        max_recommendations: int = 10,
        include_ops_checks: list[str] | None = None,
        store_evidence: bool = True,
        create_judgments: bool = True,
        score_outcomes: bool = True,
        export_vault: bool = False,
        limit_output_chars: int = 12000,
    ) -> dict[str, Any]:
        _validate_date(target_date)
        requested_symbols = _normalize_requested_symbols(symbols)
        limit_per_table = max(1, min(200, int(limit_per_table)))
        max_recommendations = max(0, min(50, int(max_recommendations)))
        checks = self._daily_checks(include_ops_checks)
        diagnostics = [
            self.run_ops_check(
                command=command,
                target_date=target_date if SAFE_OPS_CHECKS[command]["date_required"] else None,
                limit_output_chars=limit_output_chars,
            )
            for command in checks
        ]
        summary = _summarize_diagnostics(diagnostics)
        sqlite_snapshot = self.evidence_snapshot(
            target_date=target_date,
            symbols=requested_symbols,
            tables=snapshot_tables,
            limit_per_table=limit_per_table,
            store_evidence=False,
        )
        summary = _merge_sqlite_snapshot(summary, sqlite_snapshot)
        symbol_targets = _normalize_symbols(requested_symbols or summary.get("symbols") or [])
        sections = _daily_snapshot_sections(sqlite_snapshot, summary)
        brief_text = _daily_brief_text(
            target_date=target_date,
            summary=summary,
            sections=sections,
            symbol_targets=symbol_targets,
        )
        brief = {
            "title": f"Zade trading intelligence brief {target_date}",
            "text": brief_text,
            "sections": sections,
            "counts": _daily_section_counts(sections),
            "highest_value_lesson": _daily_highest_value_lesson(summary=summary, sections=sections),
        }
        evidence_record: dict[str, Any] = {}
        if store_evidence:
            evidence_record = self._record_daily_brief_evidence(
                target_date=target_date,
                diagnostics=diagnostics,
                summary=summary,
                sqlite_snapshot=sqlite_snapshot,
                brief=brief,
            )

        candidates = self._build_advisory_candidates(
            target_date=target_date,
            symbols=symbol_targets[:max_recommendations],
            summary=summary,
            sqlite_snapshot=sqlite_snapshot,
            evidence_record=evidence_record,
            priority=90,
        )
        prepared = [
            self.normalize_recommendation(candidate) | {"evidence": list(candidate.get("evidence") or [])}
            for candidate in candidates
        ]
        judgments: list[dict[str, Any]] = []
        if create_judgments:
            judgments = self._record_trading_judgments(
                target_date=target_date,
                recommendations=prepared,
                evidence_record=evidence_record,
                brief=brief,
                summary=summary,
            )

        outcome_result: dict[str, Any] = {}
        score_updates: list[dict[str, Any]] = []
        direct_score_result: dict[str, Any] = {}
        missed_calls: list[dict[str, Any]] = []
        if score_outcomes:
            outcome_result = self.score_advisory_outcomes(
                target_date=target_date,
                store_evidence=store_evidence,
                limit_output_chars=limit_output_chars,
            )
            score_updates = self._score_trading_judgments(
                target_date=target_date,
                scorecard=outcome_result.get("scorecard") or {},
            )
            missed_calls = self._record_missed_calls_from_scorecard(
                target_date=target_date,
                scorecard=outcome_result.get("scorecard") or {},
            )
            direct_score_result = self.score_judgments_against_outcomes(
                target_date=target_date,
                symbols=symbol_targets,
                store_evidence=store_evidence,
            )
            missed_calls.extend(list(direct_score_result.get("missed_calls") or []))

        vault_export: dict[str, Any] = {}
        if export_vault:
            vault_export = self.export_daily_brief_to_vault(
                target_date=target_date,
                brief=brief,
                judgments=judgments or self.list_trading_judgments(market_date=target_date, limit=200),
                outcome_score=outcome_result,
                direct_score=direct_score_result,
            )

        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.daily_brief",
            target=target_date,
            permission_tier="L2_FILE_WRITE" if export_vault else ("L1_MEMORY_WRITE" if store_evidence or create_judgments else "L0_READ"),
            status="ok",
            details={
                "commands": checks,
                "symbols": symbol_targets,
                "counts": brief["counts"],
                "evidence_id": evidence_record.get("evidence_id"),
                "judgment_count": len(judgments),
                "score_update_count": len(score_updates),
                "direct_score_update_count": len(direct_score_result.get("updates") or []),
                "missed_call_count": len(missed_calls),
                "vault_export": vault_export,
                "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
            },
        )
        return {
            "target_date": target_date,
            "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
            "authority_boundary": _daily_trading_authority_boundary(),
            "brief": brief,
            "diagnostics": diagnostics,
            "summary": summary,
            "sqlite_snapshot": _compact_snapshot(sqlite_snapshot),
            "evidence": evidence_record,
            "advisory_candidates": prepared,
            "judgments": judgments,
            "outcome_score": outcome_result,
            "score_updates": score_updates,
            "direct_score": direct_score_result,
            "missed_calls": missed_calls,
            "vault_export": vault_export,
            "audit_id": audit_id,
        }

    def list_trading_judgments(
        self,
        *,
        market_date: str | None = None,
        symbol: str | None = None,
        outcome_status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            _validate_date(market_date)
            clauses.append("market_date = ?")
            params.append(market_date)
        if symbol:
            normalized = _normalize_symbols([symbol])
            if not normalized:
                raise ValueError("symbol must be 1-16 chars of A-Z, 0-9, dot, or hyphen.")
            clauses.append("symbol = ?")
            params.append(normalized[0])
        if outcome_status:
            status = str(outcome_status or "").strip().lower()
            if status not in {"pending", "hit", "miss", "observed", "insufficient_evidence"}:
                raise ValueError("outcome_status must be pending, hit, miss, observed, or insufficient_evidence.")
            clauses.append("outcome_status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit = max(1, min(500, int(limit)))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM trading_judgments
                {where}
                ORDER BY market_date DESC, id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [_trading_judgment_from_row(row) for row in rows]

    def score_judgments_against_outcomes(
        self,
        *,
        target_date: str,
        symbols: list[str] | None = None,
        store_evidence: bool = True,
        limit_per_symbol: int = 25,
    ) -> dict[str, Any]:
        _validate_date(target_date)
        symbol_targets = _normalize_requested_symbols(symbols)
        judgments = self.list_trading_judgments(market_date=target_date, limit=500)
        if symbol_targets:
            judgments = [item for item in judgments if item["symbol"] in symbol_targets]
        limit_per_symbol = max(1, min(100, int(limit_per_symbol)))
        outcomes_by_symbol: dict[str, dict[str, Any]] = {}
        scored: list[dict[str, Any]] = []
        for symbol in sorted({item["symbol"] for item in judgments}):
            outcome_rows = self._read_realized_outcome_rows(
                target_date=target_date,
                symbol=symbol,
                limit=limit_per_symbol,
            )
            trade_rows = self._read_trade_rows(
                target_date=target_date,
                symbol=symbol,
                limit=limit_per_symbol,
            )
            outcomes_by_symbol[symbol] = {
                "outcomes": outcome_rows,
                "trades": trade_rows,
            }
        for judgment in judgments:
            symbol_data = outcomes_by_symbol.get(judgment["symbol"], {})
            scored.append(
                _direct_judgment_score(
                    judgment=judgment,
                    outcome_rows=list(symbol_data.get("outcomes") or []),
                    trade_rows=list(symbol_data.get("trades") or []),
                )
            )
        updates = self._apply_direct_judgment_scores(scored)
        missed_calls = self._record_missed_calls_from_direct_scores(target_date=target_date, scored=scored)
        evidence_record: dict[str, Any] = {}
        if store_evidence:
            evidence_record = self._record_direct_score_evidence(
                target_date=target_date,
                scored=scored,
                outcomes_by_symbol=outcomes_by_symbol,
            )
        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.judgments.score_direct",
            target=target_date,
            permission_tier="L1_MEMORY_WRITE" if store_evidence or updates else "L0_READ",
            status="ok",
            details={
                "judgment_count": len(judgments),
                "scored_count": len(scored),
                "update_count": len(updates),
                "missed_call_count": len(missed_calls),
                "symbols": sorted(outcomes_by_symbol),
                "evidence_id": evidence_record.get("evidence_id"),
                "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
            },
        )
        return {
            "target_date": target_date,
            "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
            "authority_boundary": _daily_trading_authority_boundary(),
            "scored": scored,
            "updates": updates,
            "missed_calls": missed_calls,
            "evidence": evidence_record,
            "audit_id": audit_id,
        }

    def queue_dt_trigger_proposal(
        self,
        *,
        work_queue: WorkQueueService,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        proposal = _normalize_dt_trigger_proposal(payload)
        result = work_queue.enqueue(
            kind="trading_bot_dt_trigger_proposal",
            title=f"Review dt_trigger proposal: {proposal['operation']}",
            detail=proposal["reason"],
            action=ZADE_DT_TRIGGER_PROPOSAL_ACTION,
            target=f"dt_trigger:proposal_only:{proposal['operation']}",
            permission_tier="L3_EXTERNAL_ACTION",
            priority=int(payload.get("priority", 80)),
            source="trading_bot.bridge",
            metadata={
                "proposal": proposal,
                "runtime_effect": DT_TRIGGER_PROPOSAL_RUNTIME_EFFECT,
                "evidence": list(payload.get("evidence") or []),
                "risks": list(payload.get("risks") or _default_dt_trigger_risks()),
                "authority_boundary": {
                    "proposal_only": True,
                    "runs_dt_trigger": False,
                    "runs_shell": False,
                    "trading_bot_runtime_mutation": False,
                    "broker_order_sizing_gate_mutation": False,
                },
            },
            unique_key=f"zade:dt_trigger_proposal:{proposal['idempotency_key']}",
        )
        return {
            **result.as_dict(),
            "proposal": proposal,
            "runtime_effect": DT_TRIGGER_PROPOSAL_RUNTIME_EFFECT,
        }

    def record_dt_trigger_proposal_from_work_item(self, item: WorkItem) -> dict[str, Any]:
        raw = (item.metadata or {}).get("proposal")
        if not isinstance(raw, dict):
            raise ValueError("Approved work item is missing metadata.proposal.")
        proposal = _normalize_dt_trigger_proposal(raw)
        content = _dt_trigger_proposal_markdown(proposal=proposal, work_item=item)
        memory_id = self.db.add_memory(
            kind="trading_bot_dt_trigger_proposal",
            title=f"Approved dt_trigger proposal: {proposal['operation']}",
            content=content,
            source="approval:dt_trigger_proposal",
            metadata={
                "work_item_id": item.id,
                "proposal": proposal,
                "runtime_effect": DT_TRIGGER_PROPOSAL_RUNTIME_EFFECT,
                "trading_bot_runtime_mutation": False,
            },
        )
        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action=ZADE_DT_TRIGGER_PROPOSAL_ACTION,
            target=proposal["operation"],
            permission_tier=item.permission_tier,
            status="ok",
            details={
                "work_item_id": item.id,
                "memory_id": memory_id,
                "runtime_effect": DT_TRIGGER_PROPOSAL_RUNTIME_EFFECT,
                "executed": False,
            },
        )
        return {
            "handler": ZADE_DT_TRIGGER_PROPOSAL_ACTION,
            "status": "proposal_recorded",
            "runtime_effect": DT_TRIGGER_PROPOSAL_RUNTIME_EFFECT,
            "memory_id": memory_id,
            "executed": False,
            "audit_id": audit_id,
        }

    def export_daily_brief_to_vault(
        self,
        *,
        target_date: str,
        brief: dict[str, Any],
        judgments: list[dict[str, Any]],
        outcome_score: dict[str, Any] | None = None,
        direct_score: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _validate_date(target_date)
        export_root = self.config.paths.hot_root.joinpath(*DEFAULT_TRADING_VAULT_EXPORT)
        export_root = export_root.resolve()
        hot_root = self.config.paths.hot_root.resolve()
        if hot_root != export_root and hot_root not in export_root.parents:
            raise ValueError(f"Refusing to export outside hot_root: {export_root}")
        export_root.mkdir(parents=True, exist_ok=True)
        path = export_root / f"zade-trading-brief-{target_date}.md"
        content = _daily_brief_markdown(
            target_date=target_date,
            brief=brief,
            judgments=judgments,
            outcome_score=outcome_score or {},
            direct_score=direct_score or {},
        )
        path.write_text(content, encoding="utf-8")
        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action="trading_bot.daily_brief.export_vault",
            target=str(path),
            permission_tier="L2_FILE_WRITE",
            status="ok",
            details={
                "target_date": target_date,
                "path": str(path),
                "bytes": len(content.encode("utf-8")),
                "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
            },
        )
        return {"path": str(path), "bytes": len(content.encode("utf-8")), "audit_id": audit_id}

    def deep_thought_replacement_map(self) -> dict[str, Any]:
        seams = [
            {
                "deep_thought_integration": "BotBridge.ops_check read-only diagnostics",
                "zade_replacement": "GET /trading-bot/safe-ops-checks and POST /trading-bot/ops-check",
                "status": "active",
                "authority": READ_ONLY_RUNTIME_EFFECT,
            },
            {
                "deep_thought_integration": "Deep Thought dt_recommendation client",
                "zade_replacement": "POST /trading-bot/recommendations and POST /trading-bot/advisory/generate",
                "status": "active",
                "authority": "approval_required_before_append",
            },
            {
                "deep_thought_integration": "ops_check.py dt-recommendation-outcomes",
                "zade_replacement": "POST /trading-bot/advisory/score",
                "status": "active",
                "authority": READ_ONLY_RUNTIME_EFFECT,
            },
            {
                "deep_thought_integration": "BotBridge.query_sqlite / sqlite_query",
                "zade_replacement": "GET /trading-bot/sqlite/schema, POST /trading-bot/sqlite/query, and POST /trading-bot/evidence/snapshot",
                "status": "active",
                "authority": "read_only_sqlite_no_trade_authority",
            },
            {
                "deep_thought_integration": "Deep Thought daily trading summary and vault export",
                "zade_replacement": "POST /trading-bot/daily-brief plus GET /trading-bot/judgments",
                "status": "active",
                "authority": "local_memory_write_no_trade_authority",
            },
            {
                "deep_thought_integration": "dt_trigger proposal-safe job wrapper",
                "zade_replacement": "POST /trading-bot/dt-trigger/proposals",
                "status": "active",
                "authority": DT_TRIGGER_PROPOSAL_RUNTIME_EFFECT,
            },
            {
                "deep_thought_integration": "Training, event, market-context, and signal watcher access",
                "zade_replacement": (
                    "GET /trading-bot/intelligence/access, POST /trading-bot/training/run, "
                    "GET /trading-bot/events/recent, GET /trading-bot/market-context, "
                    "and GET /trading-bot/signals/recent"
                ),
                "status": "active",
                "authority": FULL_INTELLIGENCE_RUNTIME_EFFECT,
            },
        ]
        return {
            "goal": "Replace Deep Thought trading-bot integrations with Zade-owned, local-first, authority-safe seams.",
            "active_count": sum(1 for seam in seams if seam["status"] == "active"),
            "planned_count": sum(1 for seam in seams if seam["status"] == "planned"),
            "seams": seams,
            "non_negotiables": [
                "No broker, order, sizing, gate, execution, account-risk, or runtime decision mutation.",
                "Recommendations remain approval-gated advisory bookkeeping until a separate promotion is explicitly approved.",
                "Outcome scoring reads bot reports; scored evidence is the track record for future promotion arguments.",
            ],
        }

    def ingest_recommendation_from_work_item(self, item: WorkItem) -> dict[str, Any]:
        raw = (item.metadata or {}).get("recommendation")
        if not isinstance(raw, dict):
            raise ValueError("Approved work item is missing metadata.recommendation.")
        recommendation = self.normalize_recommendation(raw)
        args = [
            self.config.trading_bot.python,
            "scripts/dt_recommendation_ingest.py",
            "--market-date",
            recommendation["market_date"],
            "--symbol",
            recommendation["symbol"],
            "--action",
            recommendation["action"],
            "--verdict",
            recommendation["verdict"],
            "--reason",
            recommendation["reason"],
            "--agent-version",
            recommendation["agent_version"],
            "--idempotency-key",
            recommendation["idempotency_key"],
            "--runtime-effect",
            DT_RECOMMENDATION_RUNTIME_EFFECT,
        ]
        if recommendation.get("conviction") is not None:
            args.extend(["--conviction", str(recommendation["conviction"])])
        if recommendation.get("context_hash"):
            args.extend(["--context-hash", str(recommendation["context_hash"])])

        result = self._run_repo_shell(_shell_join(args))
        parsed = _parse_json(result["stdout"])
        if not result["ok"]:
            raise ValueError(
                "trading-bot advisory ingest failed: "
                f"exit={result['exit_code']} stdout={result['stdout'][:500]} stderr={result['stderr'][:500]}"
            )
        audit_id = self.db.audit(
            actor="trading_bot.bridge",
            action=ZADE_DT_RECOMMENDATION_ACTION,
            target=f"{recommendation['market_date']}:{recommendation['symbol']}:{recommendation['action']}",
            permission_tier=item.permission_tier,
            status="ok",
            details={
                "work_item_id": item.id,
                "bot_result": parsed,
                "runtime_effect": DT_RECOMMENDATION_RUNTIME_EFFECT,
                "stdout": result["stdout"][:1000],
                "stderr": result["stderr"][:1000],
            },
        )
        return {
            "handler": ZADE_DT_RECOMMENDATION_ACTION,
            "status": "ok",
            "runtime_effect": DT_RECOMMENDATION_RUNTIME_EFFECT,
            "bot_result": parsed,
            "audit_id": audit_id,
        }

    def normalize_recommendation(self, payload: dict[str, Any]) -> dict[str, Any]:
        market_date = str(payload.get("market_date") or "").strip()
        _validate_date(market_date)
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not _SYMBOL_RE.match(symbol):
            raise ValueError("symbol must be 1-16 chars of A-Z, 0-9, dot, or hyphen.")
        action = str(payload.get("action") or "").strip().lower()
        if action not in VALID_RECOMMENDATION_ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(sorted(VALID_RECOMMENDATION_ACTIONS))}")
        verdict = str(payload.get("verdict") or "").strip().lower()
        if verdict not in VALID_RECOMMENDATION_VERDICTS:
            raise ValueError(f"verdict must be one of: {', '.join(sorted(VALID_RECOMMENDATION_VERDICTS))}")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise ValueError("reason is required.")
        if len(reason) > 2000:
            raise ValueError("reason must be <= 2000 chars.")
        agent_version = str(payload.get("agent_version") or "zade-local-cofounder-v1").strip()
        if not agent_version:
            raise ValueError("agent_version is required.")

        conviction = payload.get("conviction")
        if conviction is not None:
            conviction = max(0.0, min(100.0, float(conviction)))

        context_hash = str(payload.get("context_hash") or "").strip()
        if not context_hash:
            context_hash = _context_hash(
                {
                    "market_date": market_date,
                    "symbol": symbol,
                    "action": action,
                    "verdict": verdict,
                    "reason": reason,
                    "evidence": payload.get("evidence") or [],
                }
            )

        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if not idempotency_key:
            idempotency_key = _idempotency_key(market_date, symbol, action, verdict, context_hash)
        if not _IDEMPOTENCY_RE.match(idempotency_key):
            raise ValueError("idempotency_key must match ^[A-Za-z0-9_-]{8,64}$.")

        return {
            "market_date": market_date,
            "symbol": symbol,
            "action": action,
            "verdict": verdict,
            "conviction": conviction,
            "reason": reason,
            "context_hash": context_hash,
            "agent_version": agent_version,
            "idempotency_key": idempotency_key,
            "runtime_effect": DT_RECOMMENDATION_RUNTIME_EFFECT,
        }

    def _advisory_checks(self, requested: list[str] | None) -> list[str]:
        if not requested:
            return list(ADVISORY_DIAGNOSTIC_CHECKS)
        checks: list[str] = []
        for command in requested:
            command = str(command or "").strip()
            if command not in SAFE_OPS_CHECKS:
                raise ValueError(f"Unsupported trading-bot ops check: {command}")
            checks.append(command)
        return checks

    def _build_advisory_candidates(
        self,
        *,
        target_date: str,
        symbols: list[str],
        summary: dict[str, Any],
        sqlite_snapshot: dict[str, Any],
        evidence_record: dict[str, Any],
        priority: int,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for symbol in symbols:
            symbol_lines = _symbol_lines(summary, symbol)
            snapshot_rows = _snapshot_rows_for_symbol(sqlite_snapshot, symbol)
            stance = _symbol_stance(
                symbol=symbol,
                summary=summary,
                symbol_lines=symbol_lines,
                snapshot_rows=snapshot_rows,
            )
            evidence = [
                {
                    "source": "trading-bot diagnostics and read-only SQLite snapshot",
                    "evidence_id": evidence_record.get("evidence_id"),
                    "memory_id": evidence_record.get("memory_id"),
                    "summary": stance["evidence_summary"],
                    "supporting_lines": symbol_lines[:8],
                    "sqlite_rows": snapshot_rows,
                    "diagnostic_commands": summary["commands"],
                }
            ]
            reason = _limit(
                (
                    f"{stance['headline']} Evidence: {stance['evidence_summary']} "
                    "Runtime effect remains advisory_only_no_trade_authority; this cannot approve, size, route, "
                    "place, block, or cancel trades."
                ),
                1980,
            )
            context_hash = _context_hash(
                {
                    "target_date": target_date,
                    "symbol": symbol,
                    "summary": summary,
                    "stance": stance,
                    "evidence_id": evidence_record.get("evidence_id"),
                }
            )
            candidates.append(
                {
                    "market_date": target_date,
                    "symbol": symbol,
                    "action": stance["action"],
                    "verdict": stance["verdict"],
                    "conviction": stance["conviction"],
                    "reason": reason,
                    "context_hash": context_hash,
                    "agent_version": "zade-local-diagnostic-advisor-v1",
                    "evidence": evidence,
                    "risks": _default_recommendation_risks()
                    + [
                        "Diagnostic parsing is conservative and should be approved only when the founder accepts the evidence trail.",
                        "A hold/abstain advisory is still a bot database append and remains approval-gated.",
                    ],
                    "priority": priority,
                }
            )
        return candidates

    def _record_diagnostic_evidence(
        self,
        *,
        evidence_type: str,
        target_date: str,
        diagnostics: list[dict[str, Any]],
        summary: dict[str, Any],
        claim_supported: str,
        claim_contradicted: str,
    ) -> dict[str, Any]:
        title = f"{evidence_type} {target_date}"
        notes = _diagnostic_notes(target_date=target_date, diagnostics=diagnostics, summary=summary)
        memory_id = self.db.add_memory(
            kind=evidence_type,
            title=title,
            content=notes,
            source="trading-bot:diagnostics",
            metadata={
                "target_date": target_date,
                "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
                "commands": summary.get("commands", []),
                "concerns": summary.get("concerns", []),
            },
        )
        evidence_id = None
        evidence = None
        if self.founder is not None:
            result = self.founder.create_evidence(
                {
                    "evidence_type": evidence_type,
                    "source": "trading-bot read-only diagnostics",
                    "evidence_date": target_date,
                    "reliability": "B",
                    "claim_supported": claim_supported,
                    "claim_contradicted": claim_contradicted,
                    "strength": 70 if not summary.get("concerns") else 55,
                    "notes": _limit(notes, 8000),
                    "metadata": {
                        "target_date": target_date,
                        "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
                        "commands": summary.get("commands", []),
                        "diagnostics": [_compact_diagnostic(item) for item in diagnostics],
                        "summary": summary,
                    },
                }
            )
            evidence_id = result.id
            evidence = result.record
        return {"memory_id": memory_id, "evidence_id": evidence_id, "record": evidence}

    def _record_snapshot_evidence(
        self,
        *,
        target_date: str,
        symbols: list[str],
        table_results: dict[str, Any],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        title = f"trading_bot_evidence_snapshot {target_date}"
        notes = _snapshot_notes(target_date=target_date, symbols=symbols, table_results=table_results, summary=summary)
        memory_id = self.db.add_memory(
            kind="trading_bot_evidence_snapshot",
            title=title,
            content=notes,
            source="trading-bot:sqlite:read-only",
            metadata={
                "target_date": target_date,
                "symbols": symbols,
                "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
                "summary": summary,
            },
        )
        evidence_id = None
        evidence = None
        if self.founder is not None:
            result = self.founder.create_evidence(
                {
                    "evidence_type": "trading_bot_evidence_snapshot",
                    "source": "trading-bot read-only SQLite snapshot",
                    "evidence_date": target_date,
                    "reliability": "B",
                    "claim_supported": (
                        f"Zade captured {summary['total_rows']} rows of trading-bot evidence "
                        f"across {len(summary['tables'])} tables."
                    ),
                    "claim_contradicted": (
                        "No trading-bot evidence rows matched the requested date/symbol scope."
                        if summary["total_rows"] == 0
                        else ""
                    ),
                    "strength": 70 if summary["total_rows"] else 45,
                    "notes": _limit(notes, 8000),
                    "metadata": {
                        "target_date": target_date,
                        "symbols": symbols,
                        "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
                        "summary": summary,
                        "tables": _compact_snapshot_tables(table_results),
                    },
                }
            )
            evidence_id = result.id
            evidence = result.record
        return {"memory_id": memory_id, "evidence_id": evidence_id, "record": evidence}

    def _daily_checks(self, requested: list[str] | None) -> list[str]:
        if not requested:
            return list(DAILY_TRADING_CHECKS)
        checks: list[str] = []
        for command in requested:
            command = str(command or "").strip()
            if command not in SAFE_OPS_CHECKS:
                raise ValueError(f"Unsupported trading-bot ops check: {command}")
            checks.append(command)
        return checks

    def _record_daily_brief_evidence(
        self,
        *,
        target_date: str,
        diagnostics: list[dict[str, Any]],
        summary: dict[str, Any],
        sqlite_snapshot: dict[str, Any],
        brief: dict[str, Any],
    ) -> dict[str, Any]:
        counts = brief.get("counts") or {}
        title = f"trading_bot_daily_brief {target_date}"
        memory_id = self.db.add_memory(
            kind="trading_bot_daily_brief",
            title=title,
            content=str(brief.get("text") or ""),
            source="trading-bot:daily-brief",
            metadata={
                "target_date": target_date,
                "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
                "counts": counts,
                "commands": summary.get("commands", []),
                "concerns": summary.get("concerns", []),
                "sqlite_snapshot": _compact_snapshot(sqlite_snapshot),
            },
        )
        evidence_id = None
        evidence = None
        if self.founder is not None:
            total_rows = ((sqlite_snapshot.get("summary") or {}).get("total_rows") or 0)
            result = self.founder.create_evidence(
                {
                    "evidence_type": "trading_bot_daily_brief",
                    "source": "trading-bot read-only daily intelligence loop",
                    "evidence_date": target_date,
                    "reliability": "B",
                    "claim_supported": (
                        "Zade generated a local daily trading intelligence brief from read-only bot diagnostics "
                        f"and {total_rows} SQLite evidence rows."
                    ),
                    "claim_contradicted": (
                        "No date-scoped SQLite evidence rows were found for the daily trading brief."
                        if not total_rows
                        else (
                            "Trading-bot diagnostics contained concerns that limit conviction."
                            if summary.get("concerns")
                            else ""
                        )
                    ),
                    "strength": 72 if total_rows and not summary.get("concerns") else 55,
                    "notes": _limit(str(brief.get("text") or ""), 8000),
                    "metadata": {
                        "target_date": target_date,
                        "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
                        "counts": counts,
                        "commands": summary.get("commands", []),
                        "diagnostics": [_compact_diagnostic(item) for item in diagnostics],
                        "summary": summary,
                        "sqlite_snapshot": _compact_snapshot(sqlite_snapshot),
                    },
                }
            )
            evidence_id = result.id
            evidence = result.record
        return {"memory_id": memory_id, "evidence_id": evidence_id, "record": evidence}

    def _record_trading_judgments(
        self,
        *,
        target_date: str,
        recommendations: list[dict[str, Any]],
        evidence_record: dict[str, Any],
        brief: dict[str, Any],
        summary: dict[str, Any],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        created_at = utc_now()
        with self.db.connect() as conn:
            for recommendation in recommendations:
                evidence_payload = {
                    "evidence": recommendation.get("evidence") or [],
                    "daily_evidence_id": evidence_record.get("evidence_id"),
                    "daily_memory_id": evidence_record.get("memory_id"),
                    "brief_counts": brief.get("counts") or {},
                }
                evidence_hash = str(recommendation.get("context_hash") or _context_hash(evidence_payload))
                metadata = {
                    "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
                    "daily_brief_title": brief.get("title"),
                    "daily_evidence_id": evidence_record.get("evidence_id"),
                    "daily_memory_id": evidence_record.get("memory_id"),
                    "summary_symbols": summary.get("symbols", []),
                    "concerns": summary.get("concerns", [])[:20],
                    "idempotency_key": recommendation.get("idempotency_key"),
                }
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO trading_judgments (
                      created_at, market_date, symbol, action, verdict, conviction,
                      rationale, evidence_hash, evidence_json, source, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        created_at,
                        target_date,
                        recommendation["symbol"],
                        recommendation["action"],
                        recommendation["verdict"],
                        recommendation.get("conviction"),
                        recommendation.get("reason") or "",
                        evidence_hash,
                        json.dumps(evidence_payload, sort_keys=True, default=str),
                        DAILY_TRADING_JUDGMENT_SOURCE,
                        json.dumps(metadata, sort_keys=True, default=str),
                    ),
                )
                row = conn.execute(
                    """
                    SELECT * FROM trading_judgments
                    WHERE market_date = ? AND symbol = ? AND action = ? AND verdict = ? AND evidence_hash = ?
                    """,
                    (
                        target_date,
                        recommendation["symbol"],
                        recommendation["action"],
                        recommendation["verdict"],
                        evidence_hash,
                    ),
                ).fetchone()
                if row is not None:
                    records.append(_trading_judgment_from_row(row) | {"inserted": cur.rowcount == 1})
        return records

    def _score_trading_judgments(self, *, target_date: str, scorecard: dict[str, Any]) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        detail_rows = list(scorecard.get("detail_rows") or [])
        if not detail_rows:
            return updates
        with self.db.connect() as conn:
            for detail in detail_rows:
                agreement = _agreement_from_outcome_detail(str(detail.get("detail") or ""))
                if agreement is None:
                    continue
                status = "hit" if agreement else "miss"
                score = 1.0 if agreement else 0.0
                lesson = (
                    "Daily advisory judgment agreed with the bot-owned outcome scorecard."
                    if agreement
                    else "Daily advisory judgment disagreed with the bot-owned outcome scorecard; lower confidence until the miss is explained."
                )
                conn.execute(
                    """
                    UPDATE trading_judgments
                    SET outcome_status = ?, outcome_summary = ?, score = ?, lesson = ?
                    WHERE market_date = ? AND symbol = ? AND action = ? AND verdict = ?
                    """,
                    (
                        status,
                        str(detail.get("detail") or ""),
                        score,
                        lesson,
                        target_date,
                        detail.get("symbol"),
                        detail.get("action"),
                        detail.get("verdict"),
                    ),
                )
                rows = conn.execute(
                    """
                    SELECT * FROM trading_judgments
                    WHERE market_date = ? AND symbol = ? AND action = ? AND verdict = ?
                    ORDER BY id DESC
                    """,
                    (target_date, detail.get("symbol"), detail.get("action"), detail.get("verdict")),
                ).fetchall()
                updates.extend(_trading_judgment_from_row(row) for row in rows)
        return updates

    def _read_realized_outcome_rows(self, *, target_date: str, symbol: str, limit: int) -> list[dict[str, Any]]:
        result = self.run_sqlite_query(
            sql=(
                "SELECT id, timestamp, symbol, action, rejection_reason, return_5m, return_15m, "
                "return_30m, return_60m, return_eod, max_favorable_60m, max_adverse_60m, label_status "
                "FROM rejected_signal_outcomes "
                "WHERE substr(timestamp, 1, 10) = ? AND symbol = ? "
                "ORDER BY timestamp DESC, id DESC"
            ),
            params=[target_date, symbol],
            limit=limit,
            timeout_seconds=10.0,
            database="trades.db",
        )
        return [_compact_snapshot_row(row) for row in result.get("rows") or []]

    def _read_trade_rows(self, *, target_date: str, symbol: str, limit: int) -> list[dict[str, Any]]:
        result = self.run_sqlite_query(
            sql=(
                "SELECT id, timestamp, symbol, action, approved, order_status, confidence, "
                "prediction_score, setup_label, buy_opportunity_score, rejection_reason "
                "FROM trades "
                "WHERE substr(timestamp, 1, 10) = ? AND symbol = ? "
                "ORDER BY timestamp DESC, id DESC"
            ),
            params=[target_date, symbol],
            limit=limit,
            timeout_seconds=10.0,
            database="trades.db",
        )
        return [_compact_snapshot_row(row) for row in result.get("rows") or []]

    def _apply_direct_judgment_scores(self, scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        if not scored:
            return updates
        with self.db.connect() as conn:
            for item in scored:
                status = item.get("outcome_status")
                if status in {"pending", "insufficient_evidence"}:
                    continue
                current = conn.execute("SELECT outcome_status FROM trading_judgments WHERE id = ?", (item.get("judgment_id"),)).fetchone()
                if current and current["outcome_status"] in {"hit", "miss"} and status == "observed":
                    continue
                conn.execute(
                    """
                    UPDATE trading_judgments
                    SET outcome_status = ?, outcome_summary = ?, score = ?, lesson = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        item.get("outcome_summary") or "",
                        item.get("score"),
                        item.get("lesson") or "",
                        item.get("judgment_id"),
                    ),
                )
                row = conn.execute("SELECT * FROM trading_judgments WHERE id = ?", (item.get("judgment_id"),)).fetchone()
                if row is not None:
                    updates.append(_trading_judgment_from_row(row))
        return updates

    def _record_direct_score_evidence(
        self,
        *,
        target_date: str,
        scored: list[dict[str, Any]],
        outcomes_by_symbol: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        title = f"trading_bot_direct_judgment_score {target_date}"
        notes = _direct_score_notes(target_date=target_date, scored=scored, outcomes_by_symbol=outcomes_by_symbol)
        memory_id = self.db.add_memory(
            kind="trading_bot_direct_judgment_score",
            title=title,
            content=notes,
            source="trading-bot:sqlite:direct-outcome-score",
            metadata={
                "target_date": target_date,
                "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
                "counts": _direct_score_counts(scored),
                "symbols": sorted(outcomes_by_symbol),
            },
        )
        evidence_id = None
        evidence = None
        if self.founder is not None:
            counts = _direct_score_counts(scored)
            result = self.founder.create_evidence(
                {
                    "evidence_type": "trading_bot_direct_judgment_score",
                    "source": "trading-bot read-only SQLite outcome rows",
                    "evidence_date": target_date,
                    "reliability": "B",
                    "claim_supported": (
                        f"Zade directly scored {len(scored)} trading judgments against read-only outcome evidence."
                    ),
                    "claim_contradicted": (
                        "No direct realized outcome rows were available for any scored judgment."
                        if not any(item.get("outcome_evidence") for item in scored)
                        else ""
                    ),
                    "strength": 70 if counts.get("hit", 0) or counts.get("miss", 0) else 50,
                    "notes": _limit(notes, 8000),
                    "metadata": {
                        "target_date": target_date,
                        "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
                        "counts": counts,
                        "scored": scored[:100],
                    },
                }
            )
            evidence_id = result.id
            evidence = result.record
        return {"memory_id": memory_id, "evidence_id": evidence_id, "record": evidence}

    def _record_missed_calls_from_direct_scores(self, *, target_date: str, scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.founder is None:
            return []
        created: list[dict[str, Any]] = []
        for item in scored:
            if item.get("outcome_status") != "miss":
                continue
            source_hash = _context_hash(
                {
                    "target_date": target_date,
                    "judgment_id": item.get("judgment_id"),
                    "symbol": item.get("symbol"),
                    "action": item.get("action"),
                    "verdict": item.get("verdict"),
                    "summary": item.get("outcome_summary"),
                    "source": "direct-realized-outcome",
                }
            )
            if self._missed_call_review_exists(source_hash):
                continue
            result = self.founder.create_missed_call_review(
                {
                    "prediction": (
                        f"Direct trading judgment {item.get('symbol')} {item.get('action')} "
                        f"{item.get('verdict')} on {target_date}"
                    ),
                    "expected": "The judgment would agree with realized read-only outcome rows.",
                    "actual": str(item.get("outcome_summary") or ""),
                    "error_type": "trading_direct_outcome_miss",
                    "lesson": str(item.get("lesson") or "Direct realized evidence contradicted the judgment."),
                    "what_changes_now": "Require stronger realized-outcome evidence before approving similar trading advisories.",
                    "metadata": {
                        "source_hash": source_hash,
                        "target_date": target_date,
                        "direct_score": item,
                        "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
                    },
                }
            )
            created.append(result.record)
        return created

    def _record_missed_calls_from_scorecard(self, *, target_date: str, scorecard: dict[str, Any]) -> list[dict[str, Any]]:
        if self.founder is None:
            return []
        created: list[dict[str, Any]] = []
        for detail in list(scorecard.get("detail_rows") or []):
            agreement = _agreement_from_outcome_detail(str(detail.get("detail") or ""))
            if agreement is not False:
                continue
            source_hash = _context_hash(
                {
                    "target_date": target_date,
                    "symbol": detail.get("symbol"),
                    "action": detail.get("action"),
                    "verdict": detail.get("verdict"),
                    "detail": detail.get("detail"),
                    "source": "dt-recommendation-outcomes",
                }
            )
            if self._missed_call_review_exists(source_hash):
                continue
            result = self.founder.create_missed_call_review(
                {
                    "prediction": (
                        f"Trading advisory {detail.get('symbol')} {detail.get('action')} "
                        f"{detail.get('verdict')} on {target_date}"
                    ),
                    "expected": "The advisory judgment would agree with the realized bot outcome evidence.",
                    "actual": str(detail.get("detail") or ""),
                    "error_type": "trading_advisory_miss",
                    "lesson": "A bot-grounded advisory missed its realized outcome. Future conviction must require stronger evidence for this pattern.",
                    "what_changes_now": "Review the evidence rows behind this symbol before approving similar advisory recommendations.",
                    "metadata": {
                        "source_hash": source_hash,
                        "target_date": target_date,
                        "scorecard_row": detail,
                        "runtime_effect": READ_ONLY_RUNTIME_EFFECT,
                    },
                }
            )
            created.append(result.record)
        return created

    def _missed_call_review_exists(self, source_hash: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM missed_call_reviews
                WHERE metadata_json LIKE ?
                LIMIT 1
                """,
                (f"%{source_hash}%",),
            ).fetchone()
        return row is not None

    def _query_table_snapshot(
        self,
        *,
        table: str,
        spec: dict[str, Any],
        limit: int,
        target_date: str | None = None,
        symbol: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        query = _table_snapshot_query(
            table=table,
            spec=spec,
            limit=limit,
            target_date=target_date,
            symbol=symbol,
            event_type=event_type,
            since=since,
        )
        try:
            result = self.run_sqlite_query(
                sql=query["sql"],
                params=query["params"],
                limit=limit,
                timeout_seconds=10.0,
                database="trades.db",
            )
            return {"ok": True, "table": table, **result}
        except ValueError as exc:
            return {
                "ok": False,
                "table": table,
                "error": str(exc),
                "rows": [],
                "row_count": 0,
                "runtime_effect": READ_ONLY_SQLITE_RUNTIME_EFFECT,
            }

    def _read_market_context_file(self) -> dict[str, Any]:
        result = self._run_repo_shell(
            _shell_join(
                [
                    "env",
                    f"PYTHONPATH={TRADING_BOT_PYTHONPATH}",
                    self.config.trading_bot.python,
                    "-c",
                    _MARKET_CONTEXT_FILE_SCRIPT,
                ]
            ),
            timeout=10,
        )
        parsed = _parse_json_value(result["stdout"])
        payload = parsed if isinstance(parsed, dict) else {}
        return {
            "ok": bool(result["ok"] and payload),
            "exists": bool(payload.get("exists")),
            "path": payload.get("path") or "market_context.json",
            "data": payload.get("data"),
            "error": payload.get("error") or ("" if result["ok"] else _limit(result["stderr"], 1000)),
            "runtime_effect": FULL_INTELLIGENCE_RUNTIME_EFFECT,
        }

    def _run_repo_shell(self, script: str, *, timeout: float | None = None) -> dict[str, Any]:
        cfg = self.config.trading_bot
        if not cfg.enabled:
            return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "trading-bot bridge disabled"}
        command = f"cd {shlex.quote(cfg.repo_path)} && {script}"
        if os.name == "nt":
            args = ["wsl.exe", "-d", cfg.wsl_distro, "--", "bash", "-lc", command]
            cwd = None
        else:
            args = ["bash", "-lc", command]
            cwd = None
        try:
            completed = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or cfg.timeout_seconds,
                check=False,
            )
        except Exception as exc:
            return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(exc)}
        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        }


def _validate_date(value: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"date must be YYYY-MM-DD: {value!r}") from exc


def _shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def _limit(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def _compact_probe(result: dict[str, Any], *, limit: int) -> dict[str, Any]:
    return {
        "ok": result["ok"],
        "exit_code": result["exit_code"],
        "stdout": _limit(result["stdout"], limit),
        "stderr": _limit(result["stderr"], limit),
    }


def _parse_json(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return None


def _parse_json_value(stdout: str) -> Any:
    text = str(stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        parsed = _parse_json(text)
        if not parsed:
            return None
        if set(parsed) == {"value"}:
            return parsed["value"]
        return parsed


def _json_rows(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        for key in ("items", "rows", "events", "value"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [parsed]
    return []


def _sqlite_database_path(database: str) -> str:
    key = str(database or "trades.db").strip()
    if key not in TRADING_SQLITE_DATABASES:
        raise ValueError(f"Unsupported trading-bot database: {database!r}")
    return TRADING_SQLITE_DATABASES[key]


def _validate_sql_identifier(value: str | None, kind: str) -> str:
    text = str(value or "").strip()
    if not _SQL_IDENTIFIER_RE.match(text):
        raise ValueError(f"Invalid SQLite {kind} identifier: {value!r}")
    return text


def _quote_identifier(value: str) -> str:
    return '"' + _validate_sql_identifier(value, "identifier").replace('"', '""') + '"'


def _validate_sqlite_read_sql(sql: str) -> str:
    raw = str(sql or "")
    if not raw.strip():
        raise ValueError("sql is required.")
    if len(raw) > 20_000:
        raise ValueError("sql must be <= 20000 chars.")
    masked = _mask_sql_literals_and_comments(raw)
    statement = masked.strip()
    if statement.endswith(";"):
        statement = statement[:-1].strip()
    if ";" in statement:
        raise ValueError("Only one SQLite statement is allowed.")
    first = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)", statement)
    if not first:
        raise ValueError("Unable to identify SQLite statement type.")
    keyword = first.group(1).lower()
    if keyword == "pragma":
        _validate_readonly_pragma(raw)
    elif keyword not in {"select", "with", "explain"}:
        raise ValueError("Only SELECT, WITH, EXPLAIN, and read-only PRAGMA statements are allowed.")
    blocked = re.search(
        r"\b(attach|detach|insert|update|delete|replace|drop|alter|create|truncate|vacuum|"
        r"reindex|analyze|begin|commit|rollback|savepoint|release|load_extension)\b",
        statement,
        flags=re.IGNORECASE,
    )
    if blocked:
        raise ValueError(f"Blocked SQLite token: {blocked.group(1).lower()}")
    return raw.strip().rstrip(";").strip()


def _mask_sql_literals_and_comments(sql: str) -> str:
    chars = list(sql)
    i = 0
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if ch == "-" and nxt == "-":
            chars[i] = chars[i + 1] = " "
            i += 2
            while i < len(chars) and chars[i] != "\n":
                chars[i] = " "
                i += 1
            continue
        if ch == "/" and nxt == "*":
            chars[i] = chars[i + 1] = " "
            i += 2
            while i + 1 < len(chars) and not (chars[i] == "*" and chars[i + 1] == "/"):
                if chars[i] != "\n":
                    chars[i] = " "
                i += 1
            if i + 1 < len(chars):
                chars[i] = chars[i + 1] = " "
                i += 2
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            chars[i] = " "
            i += 1
            while i < len(chars):
                if chars[i] == quote:
                    chars[i] = " "
                    if i + 1 < len(chars) and chars[i + 1] == quote:
                        chars[i + 1] = " "
                        i += 2
                        continue
                    i += 1
                    break
                if chars[i] != "\n":
                    chars[i] = " "
                i += 1
            continue
        i += 1
    return "".join(chars)


def _validate_readonly_pragma(sql: str) -> None:
    if "=" in _mask_sql_literals_and_comments(sql):
        raise ValueError("Writable PRAGMA assignment is blocked.")
    match = re.match(
        r'^\s*PRAGMA\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*(?:"([A-Za-z_][A-Za-z0-9_]*)"|([A-Za-z_][A-Za-z0-9_]*))\s*\))?\s*;?\s*$',
        sql.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("Unsupported PRAGMA syntax.")
    name = match.group(1).lower()
    arg = match.group(2) or match.group(3)
    if name in {"database_list", "query_only"}:
        if arg:
            raise ValueError(f"PRAGMA {name} does not accept a table argument here.")
        return
    if name in {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}:
        _validate_sql_identifier(arg, "pragma argument")
        return
    raise ValueError(f"PRAGMA {name} is not allowlisted.")


def _validate_sqlite_params(params: list[Any] | dict[str, Any] | None) -> list[Any] | dict[str, Any]:
    if params is None:
        return []
    if isinstance(params, list):
        return [_validate_sqlite_param_value(value) for value in params]
    if isinstance(params, dict):
        return {
            _validate_sql_identifier(str(key), "parameter"): _validate_sqlite_param_value(value)
            for key, value in params.items()
        }
    raise ValueError("params must be a JSON list or object.")


def _validate_sqlite_param_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 4000:
            raise ValueError("SQLite string parameters must be <= 4000 chars.")
        return value
    raise ValueError("SQLite parameters must be scalar JSON values.")


def _validate_snapshot_tables(tables: list[str] | None) -> list[str]:
    if not tables:
        return list(TRADING_EVIDENCE_TABLES)
    normalized: list[str] = []
    for raw in tables:
        table = _validate_sql_identifier(str(raw or ""), "table")
        if table not in TRADING_EVIDENCE_TABLES:
            raise ValueError(f"Unsupported evidence snapshot table: {table}")
        if table not in normalized:
            normalized.append(table)
    return normalized


def _snapshot_query(*, table: str, target_date: str, symbols: list[str], limit: int) -> dict[str, Any]:
    spec = TRADING_EVIDENCE_TABLES[table]
    columns = ", ".join(_quote_identifier(column) for column in spec["columns"])
    date_column = _quote_identifier(spec["date_column"])
    if spec["date_column"] == "market_date":
        where = f"{date_column} = ?"
    else:
        where = f"substr({date_column}, 1, 10) = ?"
    params: list[Any] = [target_date]
    if symbols:
        placeholders = ", ".join("?" for _ in symbols)
        where += f" AND symbol IN ({placeholders})"
        params.extend(symbols)
    order_by = str(spec["order_by"])
    sql = f"SELECT {columns} FROM {_quote_identifier(table)} WHERE {where} ORDER BY {order_by} LIMIT {int(limit)}"
    return {"sql": sql, "params": params}


def _table_snapshot_query(
    *,
    table: str,
    spec: dict[str, Any],
    limit: int,
    target_date: str | None = None,
    symbol: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    columns = ", ".join(_quote_identifier(str(column)) for column in spec["columns"])
    where_parts: list[str] = []
    params: list[Any] = []

    date_column = spec.get("date_column")
    if target_date and date_column:
        quoted_date_column = _quote_identifier(str(date_column))
        if date_column == "market_date":
            where_parts.append(f"{quoted_date_column} = ?")
        else:
            where_parts.append(f"substr({quoted_date_column}, 1, 10) = ?")
        params.append(target_date)

    symbol_column = spec.get("symbol_column")
    if symbol and symbol_column:
        where_parts.append(f"{_quote_identifier(str(symbol_column))} = ?")
        params.append(symbol)

    event_type_column = spec.get("event_type_column")
    if event_type and event_type_column:
        where_parts.append(f"{_quote_identifier(str(event_type_column))} = ?")
        params.append(event_type)

    since_column = spec.get("since_column")
    if since and since_column:
        where_parts.append(f"{_quote_identifier(str(since_column))} >= ?")
        params.append(since)

    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    order_by = _format_order_by(spec.get("order_by") or [("id", "DESC")])
    sql = f"SELECT {columns} FROM {_quote_identifier(table)}{where} ORDER BY {order_by} LIMIT {int(limit)}"
    return {"sql": sql, "params": params}


def _format_order_by(items: Any) -> str:
    formatted: list[str] = []
    for item in list(items or []):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("Invalid table snapshot order_by config.")
        column = _quote_identifier(str(item[0]))
        direction = str(item[1] or "DESC").upper()
        if direction not in {"ASC", "DESC"}:
            raise ValueError("Invalid table snapshot order direction.")
        formatted.append(f"{column} {direction}")
    if not formatted:
        formatted.append(f"{_quote_identifier('id')} DESC")
    return ", ".join(formatted)


def _normalize_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen or symbol in _SYMBOL_STOPWORDS:
            continue
        if not _SYMBOL_RE.match(symbol):
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def _normalize_requested_symbols(symbols: list[str] | None) -> list[str]:
    raw_symbols = list(symbols or [])
    normalized = _normalize_symbols(raw_symbols)
    if any(str(raw or "").strip() for raw in raw_symbols) and not normalized:
        raise ValueError("No valid trading symbols supplied.")
    return normalized


def _normalize_single_symbol(symbol: str | None) -> str | None:
    if symbol is None or not str(symbol).strip():
        return None
    normalized = _normalize_symbols([str(symbol)])
    if not normalized:
        raise ValueError(f"Invalid trading symbol: {symbol!r}")
    return normalized[0]


def _validate_event_type(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if not _EVENT_TYPE_RE.match(text):
        raise ValueError(f"Invalid event_type: {value!r}")
    return text


def _validate_cli_extra_args(values: list[str] | None) -> list[str]:
    safe: list[str] = []
    for index, value in enumerate(list(values or [])):
        safe.append(_validate_cli_optional_value(value, f"extra_args[{index}]"))
    return safe


def _validate_cli_optional_value(value: str | None, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} cannot be empty.")
    if len(text) > 240:
        raise ValueError(f"{label} must be <= 240 chars.")
    if "\\" in text or ".." in text or text.startswith(("/", "~")):
        raise ValueError(f"{label} cannot reference absolute or parent paths.")
    if not _CLI_TOKEN_RE.match(text):
        raise ValueError(f"{label} contains unsupported shell characters.")
    return text


def _summarize_diagnostics(diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    facts: list[dict[str, Any]] = []
    concerns: list[dict[str, Any]] = []
    symbols: list[str] = []
    symbol_lines: dict[str, list[str]] = {}
    report_versions: dict[str, str] = {}
    for diagnostic in diagnostics:
        command = str(diagnostic.get("command") or "")
        text = "\n".join(
            part for part in [str(diagnostic.get("stdout") or ""), str(diagnostic.get("stderr") or "")] if part
        )
        current_section = ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or set(line) <= {"="}:
                continue
            lower = line.lower()
            if ":" not in line and not line.startswith("- ") and not line.startswith("["):
                current_section = lower.strip()
            if ":" in line and len(line) <= 240:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                facts.append({"command": command, "key": key, "value": value})
                if key == "report_version":
                    report_versions[command] = value
            if (
                line.startswith("[WARN]")
                or any(marker.lower() in lower for marker in _CONCERN_MARKERS)
                or (line.startswith("- ") and current_section in {"warnings", "blockers", "critical blockers"})
            ):
                concerns.append({"command": command, "line": line})
            for match in _DIAGNOSTIC_SYMBOL_RE.findall(line):
                symbol = match.upper()
                if symbol in _SYMBOL_STOPWORDS:
                    continue
                symbols.append(symbol)
                symbol_lines.setdefault(symbol, []).append(f"{command}: {line}")
    return {
        "commands": [str(item.get("command") or "") for item in diagnostics],
        "all_ok": all(bool(item.get("ok")) for item in diagnostics),
        "ok_by_command": {str(item.get("command") or ""): bool(item.get("ok")) for item in diagnostics},
        "report_versions": report_versions,
        "facts": facts[:160],
        "concerns": _dedupe_concerns(concerns)[:80],
        "symbols": _normalize_symbols(symbols),
        "symbol_lines": {symbol: lines[:20] for symbol, lines in symbol_lines.items()},
    }


def _merge_sqlite_snapshot(summary: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    merged = dict(summary)
    snapshot_summary = snapshot.get("summary") or {}
    merged["sqlite_snapshot"] = {
        "total_rows": snapshot_summary.get("total_rows", 0),
        "tables": snapshot_summary.get("tables", {}),
        "symbols": snapshot_summary.get("symbols", []),
    }
    merged["symbols"] = _normalize_symbols(list(summary.get("symbols") or []) + list(snapshot_summary.get("symbols") or []))
    return merged


def _summarize_evidence_snapshot(
    *,
    target_date: str,
    symbols: list[str],
    tables: dict[str, Any],
) -> dict[str, Any]:
    table_summary: dict[str, Any] = {}
    discovered_symbols: list[str] = []
    total_rows = 0
    for table_name, result in tables.items():
        rows = list(result.get("rows") or [])
        total_rows += len(rows)
        for row in rows:
            if row.get("symbol"):
                discovered_symbols.append(str(row["symbol"]))
        table_summary[table_name] = {
            "ok": bool(result.get("ok", True)) if "ok" in result else bool(result.get("columns")),
            "row_count": len(rows),
            "truncated": bool(result.get("truncated")),
            "error": result.get("error"),
        }
    return {
        "target_date": target_date,
        "requested_symbols": symbols,
        "symbols": _normalize_symbols(discovered_symbols),
        "total_rows": total_rows,
        "tables": table_summary,
    }


def _snapshot_rows_for_symbol(snapshot: dict[str, Any], symbol: str) -> dict[str, list[dict[str, Any]]]:
    symbol = symbol.upper()
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for table_name, result in (snapshot.get("tables") or {}).items():
        rows = [
            _compact_snapshot_row(row)
            for row in list(result.get("rows") or [])
            if str(row.get("symbol") or "").upper() == symbol
        ]
        if rows:
            rows_by_table[table_name] = rows[:8]
    return rows_by_table


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not snapshot:
        return {}
    return {
        "target_date": snapshot.get("target_date"),
        "symbols": snapshot.get("symbols", []),
        "runtime_effect": snapshot.get("runtime_effect"),
        "summary": snapshot.get("summary", {}),
        "tables": _compact_snapshot_tables(snapshot.get("tables") or {}),
    }


def _compact_snapshot_tables(table_results: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for table_name, result in table_results.items():
        compact[table_name] = {
            "row_count": len(result.get("rows") or []),
            "truncated": bool(result.get("truncated")),
            "columns": list(result.get("columns") or [])[:20],
            "rows": [_compact_snapshot_row(row) for row in list(result.get("rows") or [])[:10]],
            "error": result.get("error"),
        }
    return compact


def _compact_snapshot_row(row: dict[str, Any]) -> dict[str, Any]:
    keep = [
        "id",
        "timestamp",
        "created_at",
        "decision_time",
        "market_date",
        "symbol",
        "action",
        "decision",
        "final_decision",
        "approved",
        "order_submitted",
        "order_status",
        "score",
        "prediction_score",
        "setup_score",
        "buy_opportunity_score",
        "verdict",
        "conviction",
        "reason",
        "rejection_reason",
        "hard_block_reason",
        "return_5m",
        "return_15m",
        "return_30m",
        "return_60m",
        "return_eod",
        "max_favorable_60m",
        "max_adverse_60m",
        "label_status",
        "runtime_effect",
    ]
    return {key: row.get(key) for key in keep if key in row}


def _dedupe_concerns(concerns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for concern in concerns:
        key = (str(concern.get("command") or ""), str(concern.get("line") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(concern)
    return deduped


def _symbol_lines(summary: dict[str, Any], symbol: str) -> list[str]:
    return list((summary.get("symbol_lines") or {}).get(symbol, []))


def _symbol_stance(
    *,
    symbol: str,
    summary: dict[str, Any],
    symbol_lines: list[str],
    snapshot_rows: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    concerns = list(summary.get("concerns") or [])
    concern_text = " | ".join(str(item.get("line") or "") for item in concerns[:6])
    critical = any(
        any(marker in str(item.get("line") or "").lower() for marker in ("blocker", "missing", "error", "false"))
        for item in concerns
    )
    snapshot_facts = _symbol_snapshot_facts(snapshot_rows)
    positive_symbol_line = any(
        any(marker in line.lower() for marker in _POSITIVE_SYMBOL_MARKERS) for line in symbol_lines
    )
    if critical:
        return {
            "action": "hold",
            "verdict": "abstain",
            "conviction": 62.0,
            "headline": f"Zade abstains on {symbol}; the diagnostics are not clean enough for a stronger advisory.",
            "evidence_summary": _join_evidence_summary(
                concern_text or "A diagnostic concern was detected.",
                snapshot_facts["summary"],
            ),
        }
    if snapshot_facts["approved_count"] > 0:
        return {
            "action": "buy",
            "verdict": "recommend",
            "conviction": snapshot_facts["conviction"],
            "headline": f"Zade recommends a paper-only advisory buy stance for {symbol}.",
            "evidence_summary": snapshot_facts["summary"],
        }
    if snapshot_facts["rejected_count"] > 0 and snapshot_facts["approved_count"] == 0:
        return {
            "action": "buy",
            "verdict": "against",
            "conviction": max(60.0, min(72.0, snapshot_facts["conviction"])),
            "headline": f"Zade recommends against a paper buy stance for {symbol}.",
            "evidence_summary": snapshot_facts["summary"],
        }
    if snapshot_facts["row_count"] > 0:
        return {
            "action": "hold",
            "verdict": "abstain",
            "conviction": 60.0,
            "headline": f"Zade abstains on {symbol}; table evidence exists but does not clear the advisory bar.",
            "evidence_summary": snapshot_facts["summary"],
        }
    if not symbol_lines:
        return {
            "action": "hold",
            "verdict": "abstain",
            "conviction": 54.0,
            "headline": f"Zade abstains on {symbol}; diagnostics are real but not symbol-specific.",
            "evidence_summary": "No parsed diagnostic line specifically supported this symbol.",
        }
    if positive_symbol_line and not concerns:
        return {
            "action": "buy",
            "verdict": "recommend",
            "conviction": 66.0,
            "headline": f"Zade recommends a paper-only advisory buy stance for {symbol}.",
            "evidence_summary": "Symbol-specific diagnostic lines were positive and no run-level concerns were found.",
        }
    return {
        "action": "hold",
        "verdict": "abstain",
        "conviction": 58.0,
        "headline": f"Zade abstains on {symbol}; evidence exists but does not clear the advisory bar.",
        "evidence_summary": "Symbol-specific lines exist, but they were not strong enough for a recommend/against stance.",
    }


def _symbol_snapshot_facts(snapshot_rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    rows = [row for table_rows in snapshot_rows.values() for row in table_rows]
    approved_count = sum(1 for row in rows if _truthy(row.get("approved")) or _truthy(row.get("order_submitted")))
    rejected_count = sum(
        1
        for row in rows
        if str(row.get("decision") or row.get("final_decision") or "").lower() == "rejected"
        or bool(row.get("rejection_reason") or row.get("hard_block_reason"))
    )
    scores = [
        float(value)
        for row in rows
        for value in [
            row.get("score"),
            row.get("prediction_score"),
            row.get("setup_score"),
            row.get("buy_opportunity_score"),
        ]
        if _is_number(value)
    ]
    max_score = max(scores) if scores else None
    conviction = 62.0
    if max_score is not None:
        conviction = max(55.0, min(78.0, float(max_score)))
    fragments = [
        f"{len(rows)} SQLite evidence rows",
        f"approved/order_submitted={approved_count}",
        f"rejected/blocker={rejected_count}",
    ]
    if max_score is not None:
        fragments.append(f"max_score={round(max_score, 2)}")
    if snapshot_rows:
        fragments.append("tables=" + ",".join(sorted(snapshot_rows)))
    return {
        "row_count": len(rows),
        "approved_count": approved_count,
        "rejected_count": rejected_count,
        "max_score": max_score,
        "conviction": conviction,
        "summary": "; ".join(fragments),
    }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "approved", "filled", "submitted"}


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return value is not None
    except (TypeError, ValueError):
        return False


def _join_evidence_summary(*parts: str) -> str:
    return " ".join(part for part in parts if part)


def _parse_outcome_scorecard(diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(str(item.get("stdout") or "") for item in diagnostics)
    recommendations = 0
    verdicts: dict[str, dict[str, Any]] = {}
    detail_rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        rec_match = re.match(r"recommendations\s*:\s*(\d+)", stripped)
        if rec_match:
            recommendations = max(recommendations, int(rec_match.group(1)))
        verdict_match = re.match(
            r"^(recommend|against|abstain)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)$",
            stripped,
        )
        if verdict_match:
            verdicts[verdict_match.group(1)] = {
                "n": int(verdict_match.group(2)),
                "agreement_rate": _score_value(verdict_match.group(3)),
                "realized_avg": _score_value(verdict_match.group(4)),
                "counterfactual_60m_avg": _score_value(verdict_match.group(5)),
            }
        detail_match = re.match(
            r"^([A-Z0-9.-]{1,16})\s+(buy|sell|hold)\s+(recommend|against|abstain)\s+(.+)$",
            stripped,
        )
        if detail_match:
            detail_rows.append(
                {
                    "symbol": detail_match.group(1),
                    "action": detail_match.group(2),
                    "verdict": detail_match.group(3),
                    "detail": detail_match.group(4),
                }
            )
    return {
        "recommendations": recommendations,
        "by_verdict": verdicts,
        "detail_rows": detail_rows[:50],
    }


def _score_value(value: str) -> float | None:
    if value in {"-", "None", "none", "null"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _diagnostic_notes(*, target_date: str, diagnostics: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        f"Trading-bot diagnostic evidence for {target_date}.",
        f"Runtime effect: {READ_ONLY_RUNTIME_EFFECT}.",
        f"Commands: {', '.join(summary.get('commands') or [])}.",
        f"All diagnostics ok: {summary.get('all_ok')}.",
    ]
    concerns = summary.get("concerns") or []
    if concerns:
        lines.append("Concerns:")
        lines.extend(f"- {item['command']}: {item['line']}" for item in concerns[:20])
    facts = summary.get("facts") or []
    if facts:
        lines.append("Key facts:")
        lines.extend(f"- {item['command']} {item['key']}: {item['value']}" for item in facts[:40])
    snapshot = summary.get("sqlite_snapshot") or {}
    if snapshot:
        lines.append("SQLite snapshot:")
        lines.append(f"- runtime_effect: {READ_ONLY_SQLITE_RUNTIME_EFFECT}")
        lines.append(f"- total_rows: {snapshot.get('total_rows', 0)}")
        lines.append(f"- symbols: {', '.join(snapshot.get('symbols') or [])}")
        for table_name, table_summary in (snapshot.get("tables") or {}).items():
            lines.append(f"- {table_name}: rows={table_summary.get('row_count', 0)} truncated={table_summary.get('truncated', False)}")
    lines.append("Outputs:")
    for diagnostic in diagnostics:
        lines.append(f"--- {diagnostic.get('command')} ---")
        lines.append(_limit(str(diagnostic.get("stdout") or ""), 4000))
        stderr = str(diagnostic.get("stderr") or "")
        if stderr:
            lines.append("stderr:")
            lines.append(_limit(stderr, 1000))
    return "\n".join(lines)


def _snapshot_notes(
    *,
    target_date: str,
    symbols: list[str],
    table_results: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    lines = [
        f"Trading-bot read-only SQLite evidence snapshot for {target_date}.",
        f"Runtime effect: {READ_ONLY_SQLITE_RUNTIME_EFFECT}.",
        f"Requested symbols: {', '.join(symbols) if symbols else 'all symbols within capped table queries'}.",
        f"Total rows: {summary.get('total_rows', 0)}.",
    ]
    for table_name, table_summary in (summary.get("tables") or {}).items():
        lines.append(
            f"- {table_name}: rows={table_summary.get('row_count', 0)} "
            f"truncated={table_summary.get('truncated', False)} error={table_summary.get('error') or '-'}"
        )
    compact = _compact_snapshot_tables(table_results)
    lines.append("Rows:")
    for table_name, result in compact.items():
        lines.append(f"--- {table_name} ---")
        for row in result.get("rows", []):
            lines.append(json.dumps(row, sort_keys=True, default=str))
    return "\n".join(lines)


def _direct_judgment_score(
    *,
    judgment: dict[str, Any],
    outcome_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    outcome = _best_return_outcome(outcome_rows)
    status = "pending"
    score: float | None = None
    summary = ""
    lesson = ""
    if outcome:
        value = outcome["return"]
        action = str(judgment.get("action") or "").lower()
        verdict = str(judgment.get("verdict") or "").lower()
        direction = _return_direction(value, action)
        if verdict == "recommend":
            status = "hit" if direction == "favorable" else "miss"
            score = 1.0 if status == "hit" else 0.0
        elif verdict == "against":
            status = "hit" if direction == "unfavorable" else "miss"
            score = 1.0 if status == "hit" else 0.0
        else:
            status = "observed"
            score = 0.5
        summary = (
            f"{outcome['field']}={round(value, 6)} from rejected_signal_outcomes "
            f"row {outcome['row'].get('id')} label={outcome['row'].get('label_status') or '-'}"
        )
        if status == "hit":
            lesson = "Direct realized outcome evidence supported the judgment."
        elif status == "miss":
            lesson = "Direct realized outcome evidence contradicted the judgment."
        else:
            lesson = "Abstention was observed against realized evidence; use it for calibration, not promotion."
    elif trade_rows:
        filled = [row for row in trade_rows if _truthy(row.get("approved")) or str(row.get("order_status") or "").lower() == "filled"]
        status = "observed" if filled else "insufficient_evidence"
        score = 0.5 if filled else None
        summary = (
            f"{len(trade_rows)} trade rows found; {len(filled)} approved/filled rows; "
            "no direct return outcome row available."
        )
        lesson = "Trade evidence exists without realized return rows; score remains observational."
    else:
        status = "pending"
        summary = "No direct realized outcome or trade rows found yet."
        lesson = "Leave pending until outcome evidence exists."
    return {
        "judgment_id": judgment["id"],
        "market_date": judgment["market_date"],
        "symbol": judgment["symbol"],
        "action": judgment["action"],
        "verdict": judgment["verdict"],
        "outcome_status": status,
        "score": score,
        "outcome_summary": summary,
        "lesson": lesson,
        "outcome_evidence": {
            "outcome_rows": outcome_rows[:5],
            "trade_rows": trade_rows[:5],
        },
    }


def _best_return_outcome(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        for field in ("return_60m", "return_eod", "return_30m", "return_15m", "return_5m"):
            value = row.get(field)
            if _is_number(value):
                return {"field": field, "return": float(value), "row": row}
    return None


def _return_direction(value: float, action: str) -> str:
    if abs(value) < 0.00001:
        return "flat"
    if action == "sell":
        return "favorable" if value < 0 else "unfavorable"
    return "favorable" if value > 0 else "unfavorable"


def _direct_score_counts(scored: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"hit": 0, "miss": 0, "observed": 0, "insufficient_evidence": 0, "pending": 0}
    for item in scored:
        status = str(item.get("outcome_status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _direct_score_notes(
    *,
    target_date: str,
    scored: list[dict[str, Any]],
    outcomes_by_symbol: dict[str, dict[str, Any]],
) -> str:
    counts = _direct_score_counts(scored)
    lines = [
        f"Direct trading-judgment outcome scoring for {target_date}.",
        f"Runtime effect: {READ_ONLY_RUNTIME_EFFECT}.",
        f"SQLite effect: {READ_ONLY_SQLITE_RUNTIME_EFFECT}.",
        f"Counts: {json.dumps(counts, sort_keys=True)}.",
    ]
    for item in scored[:80]:
        lines.append(
            f"- #{item['judgment_id']} {item['symbol']} {item['action']} {item['verdict']} "
            f"=> {item['outcome_status']} score={item.get('score')} {item.get('outcome_summary')}"
        )
    if outcomes_by_symbol:
        lines.append("Evidence rows by symbol:")
        for symbol, data in sorted(outcomes_by_symbol.items()):
            lines.append(
                f"- {symbol}: outcomes={len(data.get('outcomes') or [])} trades={len(data.get('trades') or [])}"
            )
    return "\n".join(lines)


def _daily_brief_markdown(
    *,
    target_date: str,
    brief: dict[str, Any],
    judgments: list[dict[str, Any]],
    outcome_score: dict[str, Any],
    direct_score: dict[str, Any],
) -> str:
    lines = [
        "---",
        f"title: Zade Trading Brief {target_date}",
        f"date: {target_date}",
        "source: zade-local-cofounder",
        f"runtime_effect: {READ_ONLY_RUNTIME_EFFECT}",
        "---",
        "",
        f"# Zade Trading Brief - {target_date}",
        "",
        str(brief.get("text") or ""),
        "",
        "## Judgment Ledger",
    ]
    if judgments:
        for item in judgments[:100]:
            lines.append(
                f"- #{item.get('id')} {item.get('symbol')} {item.get('action')} {item.get('verdict')} "
                f"conviction={item.get('conviction')} outcome={item.get('outcome_status')} score={item.get('score')}"
            )
    else:
        lines.append("- No judgments recorded.")
    lines.extend(["", "## Outcome Scoring"])
    scorecard = outcome_score.get("scorecard") or {}
    if scorecard:
        lines.append("Bot-owned dt recommendation scorecard:")
        lines.append("```json")
        lines.append(json.dumps(scorecard, indent=2, sort_keys=True, default=str))
        lines.append("```")
    direct_updates = direct_score.get("updates") or []
    if direct_updates:
        lines.append("Direct read-only SQLite outcome updates:")
        for item in direct_updates[:100]:
            lines.append(
                f"- #{item.get('id')} {item.get('symbol')} {item.get('outcome_status')} "
                f"score={item.get('score')} {item.get('outcome_summary')}"
            )
    else:
        lines.append("- No direct score updates.")
    lines.extend(
        [
            "",
            "## Authority",
            "- Observe-only.",
            "- No broker, order, sizing, gate, execution, account-risk, or runtime mutation.",
            "- Trading-bot reads are SQLite mode=ro with PRAGMA query_only.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _normalize_dt_trigger_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    operation = str(payload.get("operation") or "").strip()
    if not operation or len(operation) > 160:
        raise ValueError("operation is required and must be <= 160 chars.")
    if any(phrase in operation.lower() for phrase in ("place order", "live trade", "broker", "account risk")):
        raise ValueError("dt_trigger proposal names a denied live-trading boundary.")
    target_date = str(payload.get("target_date") or "").strip()
    if target_date:
        _validate_date(target_date)
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise ValueError("reason is required.")
    if len(reason) > 3000:
        raise ValueError("reason must be <= 3000 chars.")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object.")
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    context_hash = _context_hash(
        {
            "operation": operation,
            "target_date": target_date,
            "reason": reason,
            "params": params,
        }
    )
    if not idempotency_key:
        safe_operation = re.sub(r"[^A-Za-z0-9_-]+", "_", operation).strip("_") or "proposal"
        idempotency_key = f"zade_dt_trigger_{safe_operation}_{context_hash[:16]}"[:64]
    if not _IDEMPOTENCY_RE.match(idempotency_key):
        raise ValueError("idempotency_key must match ^[A-Za-z0-9_-]{8,64}$.")
    return {
        "operation": operation,
        "target_date": target_date,
        "reason": reason,
        "params": params,
        "context_hash": context_hash,
        "idempotency_key": idempotency_key,
        "runtime_effect": DT_TRIGGER_PROPOSAL_RUNTIME_EFFECT,
    }


def _dt_trigger_proposal_markdown(*, proposal: dict[str, Any], work_item: WorkItem) -> str:
    return "\n".join(
        [
            f"# Approved dt_trigger Proposal - {proposal['operation']}",
            "",
            f"Runtime effect: {DT_TRIGGER_PROPOSAL_RUNTIME_EFFECT}",
            f"Work item: {work_item.id}",
            f"Target date: {proposal.get('target_date') or '-'}",
            "",
            "## Reason",
            proposal["reason"],
            "",
            "## Params",
            "```json",
            json.dumps(proposal.get("params") or {}, indent=2, sort_keys=True, default=str),
            "```",
            "",
            "## Boundary",
            "- Proposal recorded locally.",
            "- No dt_trigger process was run.",
            "- No broker, order, sizing, gate, execution, account-risk, or runtime mutation.",
        ]
    )


def _daily_snapshot_sections(snapshot: dict[str, Any], summary: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    sections: dict[str, list[dict[str, Any]]] = {"strong": [], "watch": [], "blocked": [], "noise": []}
    for concern in list(summary.get("concerns") or [])[:20]:
        sections["blocked"].append(
            {
                "kind": "diagnostic_concern",
                "source": concern.get("command"),
                "symbol": "",
                "reason": str(concern.get("line") or ""),
            }
        )
    for table_name, result in (snapshot.get("tables") or {}).items():
        if result.get("error"):
            sections["noise"].append(
                {
                    "kind": "table_error",
                    "source": table_name,
                    "symbol": "",
                    "reason": str(result.get("error") or ""),
                }
            )
        for row in list(result.get("rows") or [])[:50]:
            compact = _compact_snapshot_row(row)
            bucket = _classify_daily_row(compact)
            sections[bucket].append(
                {
                    "kind": "sqlite_row",
                    "source": table_name,
                    "symbol": str(compact.get("symbol") or ""),
                    "reason": _daily_row_reason(compact),
                    "row": compact,
                }
            )
    if not any(sections.values()):
        sections["noise"].append(
            {
                "kind": "empty_snapshot",
                "source": "trading-bot:sqlite",
                "symbol": "",
                "reason": "No date-scoped diagnostic rows or concerns were found.",
            }
        )
    return {name: items[:50] for name, items in sections.items()}


def _classify_daily_row(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in row).lower()
    decision = str(row.get("decision") or row.get("final_decision") or "").strip().lower()
    order_status = str(row.get("order_status") or "").strip().lower()
    has_rejection_text = bool(row.get("rejection_reason") or row.get("hard_block_reason"))
    if has_rejection_text and (
        _truthy(row.get("approved"))
        or _truthy(row.get("order_submitted"))
        or order_status in {"submitted", "filled", "open", "accepted"}
    ):
        return "watch"
    if (
        _truthy(row.get("approved"))
        or _truthy(row.get("order_submitted"))
        or decision in {"approved", "accepted"}
        or order_status in {"submitted", "filled", "open", "accepted"}
    ):
        return "strong"
    if (
        decision in {"rejected", "blocked", "denied"}
        or has_rejection_text
        or any(marker in text for marker in ("rejected", "blocked", "hard_block", "no_trade", "risk_block"))
    ):
        return "blocked"
    if row.get("symbol"):
        return "watch"
    return "noise"


def _daily_row_reason(row: dict[str, Any]) -> str:
    parts = []
    for key in (
        "symbol",
        "action",
        "decision",
        "final_decision",
        "approved",
        "order_submitted",
        "order_status",
        "score",
        "prediction_score",
        "setup_score",
        "buy_opportunity_score",
        "verdict",
        "conviction",
        "reason",
        "rejection_reason",
        "hard_block_reason",
        "return_60m",
        "return_eod",
    ):
        if key in row and row.get(key) not in (None, ""):
            parts.append(f"{key}={row.get(key)}")
    return "; ".join(parts[:14]) or "Evidence row present."


def _daily_section_counts(sections: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {name: len(items) for name, items in sections.items()}


def _daily_brief_text(
    *,
    target_date: str,
    summary: dict[str, Any],
    sections: dict[str, list[dict[str, Any]]],
    symbol_targets: list[str],
) -> str:
    counts = _daily_section_counts(sections)
    lines = [
        f"Trading intelligence brief for {target_date}.",
        f"Runtime effect: {FULL_INTELLIGENCE_RUNTIME_EFFECT}.",
        "Authority: full intelligence access. No broker, order, sizing, gate, execution, account-risk, or runtime mutation.",
        f"Commands: {', '.join(summary.get('commands') or [])}.",
        f"Symbols: {', '.join(symbol_targets) if symbol_targets else 'none discovered'}.",
        f"Counts: strong={counts['strong']} watch={counts['watch']} blocked={counts['blocked']} noise={counts['noise']}.",
        "",
    ]
    for section_name in ("strong", "watch", "blocked", "noise"):
        lines.append(section_name.upper())
        items = sections.get(section_name) or []
        if not items:
            lines.append("- none")
        else:
            lines.extend(_format_daily_section_item(item) for item in items[:12])
        lines.append("")
    lesson = _daily_highest_value_lesson(summary=summary, sections=sections)
    lines.append(f"Highest-value lesson: {lesson}")
    return "\n".join(lines).strip()


def _format_daily_section_item(item: dict[str, Any]) -> str:
    source = item.get("source") or "unknown"
    symbol = item.get("symbol") or "-"
    reason = _limit(str(item.get("reason") or ""), 600).replace("\n", " ")
    return f"- {symbol} [{source}] {reason}"


def _daily_highest_value_lesson(*, summary: dict[str, Any], sections: dict[str, list[dict[str, Any]]]) -> str:
    if sections.get("blocked"):
        return "Resolve the strongest blocker before treating any candidate as approval-worthy."
    if sections.get("strong"):
        return "Compare strong rows against realized outcomes before increasing future conviction."
    if summary.get("concerns"):
        return "Diagnostics contain concerns; preserve abstention until the concern is explained."
    if sections.get("watch"):
        return "Watch rows need outcome evidence before they become recommendations."
    return "No actionable evidence surfaced; improve intake coverage before adding judgment."


def _daily_trading_authority_boundary() -> dict[str, Any]:
    return {
        "intelligence": FULL_INTELLIGENCE_RUNTIME_EFFECT,
        "diagnostics": READ_ONLY_RUNTIME_EFFECT,
        "sqlite": READ_ONLY_SQLITE_RUNTIME_EFFECT,
        "writes": ["memories", "founder_evidence", "trading_judgments", "missed_call_reviews"],
        "trading_bot_database_write": False,
        "broker_order_sizing_gate_mutation": False,
        "approval_required_before_bot_append": True,
        **_bridge_authority_scope(),
    }


def _compact_diagnostic(diagnostic: dict[str, Any]) -> dict[str, Any]:
    return {
        "command": diagnostic.get("command"),
        "target_date": diagnostic.get("target_date"),
        "ok": diagnostic.get("ok"),
        "exit_code": diagnostic.get("exit_code"),
        "stdout": _limit(str(diagnostic.get("stdout") or ""), 4000),
        "stderr": _limit(str(diagnostic.get("stderr") or ""), 1000),
        "runtime_effect": diagnostic.get("runtime_effect"),
    }


def _authority_boundary() -> dict[str, Any]:
    return {
        "recommendation_rows_are_runtime_inputs": False,
        "broker_order_sizing_gate_mutation": False,
        "approval_required_before_bot_append": True,
        "bot_final_validation": "scripts/dt_recommendation_ingest.py",
        **_bridge_authority_scope(),
    }


def _full_intelligence_authority_boundary() -> dict[str, Any]:
    return {
        "scope": "training_advisory_events_market_context_signals_database_visibility",
        "training_scripts": "allowlisted_bot_training_commands",
        "advisory_writes": "approval-gated dt_recommendations append only",
        "database_reads": "SQLite mode=ro with query_only for direct table reads",
        "event_reads": "bot_events script or read-only SQLite fallback",
        "market_context_reads": "market_context.json plus daily_symbol_context read-only snapshot",
        "signal_watch": "read-only snapshots of signal/event/decision tables",
        "broker_order_sizing_gate_mutation": False,
        "runtime_decision_mutation": False,
        "account_risk_mutation": False,
        **_bridge_authority_scope(),
    }


def _sqlite_authority_boundary() -> dict[str, Any]:
    return {
        "database_open_mode": "mode=ro",
        "sqlite_query_only": True,
        "allowed_statements": ["SELECT", "WITH", "EXPLAIN", "read-only PRAGMA"],
        "write_schema_or_attachment_tokens_blocked": True,
        "broker_order_sizing_gate_mutation": False,
        "runtime_decision_mutation": False,
        **_bridge_authority_scope(),
    }


def _no_symbol_skip(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "reason": "No safe symbol target was supplied or discovered in diagnostics.",
        "required_next_input": "Call with symbols=[...] or add a symbol-producing read-only diagnostic.",
        "concerns": summary.get("concerns", [])[:10],
    }


def _context_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _trading_judgment_from_row(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": row["created_at"],
        "market_date": row["market_date"],
        "symbol": row["symbol"],
        "action": row["action"],
        "verdict": row["verdict"],
        "conviction": row["conviction"],
        "rationale": row["rationale"],
        "evidence_hash": row["evidence_hash"],
        "evidence": json.loads(row["evidence_json"] or "{}"),
        "outcome_status": row["outcome_status"],
        "outcome_summary": row["outcome_summary"],
        "score": row["score"],
        "lesson": row["lesson"],
        "source": row["source"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }


def _agreement_from_outcome_detail(detail: str) -> bool | None:
    match = re.search(r"\bagree\s*=\s*(true|false)\b", detail, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower() == "true"


def _idempotency_key(market_date: str, symbol: str, action: str, verdict: str, context_hash: str) -> str:
    safe_symbol = re.sub(r"[^A-Za-z0-9_-]+", "_", symbol).strip("_") or "symbol"
    digest = hashlib.sha256(f"{market_date}:{symbol}:{action}:{verdict}:{context_hash}".encode("utf-8")).hexdigest()
    return f"zade_{market_date.replace('-', '')}_{safe_symbol}_{digest[:20]}"[:64]


def _default_recommendation_risks() -> list[str]:
    return [
        "Advisory rows may inform intelligence review and training, but must not become broker/order runtime input without a separate promotion.",
        "The bot must perform final symbol, runtime_effect, rate-limit, and idempotency validation.",
        "Founder approval is required before Zade writes into the trading-bot database.",
    ]


def _default_dt_trigger_risks() -> list[str]:
    return [
        "This records a proposal only; it must not run dt_trigger or any shell command.",
        "Any future bot job execution requires a separate operator-controlled implementation and approval path.",
        "The proposal cannot approve, block, size, route, place, cancel, or mutate trades.",
    ]
