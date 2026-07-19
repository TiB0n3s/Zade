from __future__ import annotations

import atexit
from contextlib import asynccontextmanager
from dataclasses import asdict
import hmac
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .approval import ApprovalService
from .authority import AuthorityPolicy, AuthorityRequest, build_self_inventory
from .autonomy import WorkQueueService
from .brief import build_daily_brief
from .browser import BrowserService
from .config import KernelConfig, ensure_local_paths, load_config
from .connectors import ConnectorService
from .conversation import ConversationService
from .critic import ContrarianCritic
from .db import KernelDatabase, utc_now
from .devtools import DevToolsHandlers, allowed_commands
from .evals import EvalService
from .experiments import ExperimentService
from .anthropic_build import AnthropicBuildModelClient
from .anthropic_client import (
    AnthropicClient,
    AnthropicError,
    AnthropicNotConfigured,
    AnthropicPolicyError,
)
from .build_assessment import BuildAssessmentService
from .build_budget import BuildBudgetExceeded, BuildBudgetService
from .build_calibration import BuildCalibrationService, ManagedAgentsReadinessService
from .build_workspace import BuildWorkspacePolicy
from .build_orchestrator import BuildOrchestrator, BuildPlanner
from .build_routing import BuildRouter
from .build_service import BUILD_LEASE_SOURCE, BUILD_UPGRADE_SOURCE, BuildService
from .build_store import BuildStore
from .build_types import BuildTaskKind, BuildTier
from .build_verification import BuildVerificationService
from .build_workers import BuildExecutionManager
from .channel_auth import ChannelAuth, ChannelAuthError, parse_bind_command
from .openclaw_bridge import InboundMessage, OpenClawBridge, OpenClawBridgeError
from .heartbeat import KernelHeartbeat
from .telegram_adapter import (
    InboundTelegram,
    TelegramAdapter,
    TelegramClient,
    TelegramError,
    token_from_env,
)
from .founder import FounderService
from .strategy_review import StrategyReviewService
from .handlers import ActionHandlerRegistry
from .ingestion import IngestionService
from .research import ResearchService
from .roles import RolePassService
from .delegation import DelegationService
from .coding_agent import CodingAgentService
from .command_runner import GovernedCommandRunner, coding_agent_command_policies
from .egress import (
    DataClass,
    EgressPolicy,
    EgressRequest,
    authorize_build_egress,
)
from .github_ci import GitHubAuthorizationRequest, GitHubCIClient, GitHubCIError
from .inventory import ModelInventoryService
from .openai_review import OpenAIReviewClient, OpenAIReviewUnavailable
from .toolchain_profiles import ToolchainRegistry
from .screen import ScreenService
from .models import (
    ActionPlanCreate,
    ActionPlanFromRecommendation,
    ActionStepApprove,
    ActionStepComplete,
    ActionStepEvidenceAttach,
    ActionStepFail,
    ActionStepSkip,
    ActiveObjectiveCreate,
    ActiveObjectiveStatusUpdate,
    AssumptionCreate,
    ApprovalDeferRequest,
    ApprovalEditRequest,
    ApprovalResolveRequest,
    ChannelConfirmRequest,
    ChannelEnrollRequest,
    ChannelMessageRequest,
    ChannelTierRequest,
    StrategyReviewRequest,
    AuthorityEvaluateRequest,
    BackupCreateRequest,
    BackupRetentionRequest,
    BrowserRunRequest,
    BuildAssessRequest,
    BuildCalibrationRequest,
    BuildLeaseApproveRequest,
    BuildLeaseDenyRequest,
    BuildQuarantineRequest,
    BuildPlanRequest,
    BuildTaskCreateRequest,
    BuildTaskRetryRequest,
    BuildVerifyRequest,
    GitHubRunCancelRequest,
    GitHubWorkflowDispatchRequest,
    OpenAIReviewRunRequest,
    ResearchDaydreamRequest,
    ResearchRunRequest,
    RolePassRequest,
    DelegationBriefRequest,
    DelegationQueueRequest,
    ScreenCaptureRequest,
    VaultDeleteRequest,
    VaultMoveRequest,
    VaultRestoreRequest,
    CadenceReviewCreate,
    CadenceRunRequest,
    ChatRequest,
    ChatResponse,
    CommitmentClose,
    CommitmentCreate,
    CommitmentRenegotiate,
    CompanyThesisUpsert,
    ConnectorCreate,
    ConnectorUpdate,
    ConnectorItemDismiss,
    ConnectorItemsImport,
    ContrarianReviewCreate,
    ConversationCreate,
    DeepThoughtImportRequest,
    DeepThoughtLinkRequest,
    DeepThoughtScanRequest,
    DecisionEngineRequest,
    DecisionMemoCreate,
    EvalCaseUpsert,
    EvalRunRequest,
    EvidenceCreate,
    EvidenceLoopRequest,
    ExperimentCreate,
    ExperimentEvidenceCreate,
    ExperimentLoopRequest,
    ExperimentPushbackCreate,
    ExperimentReviewCreate,
    ExperimentUpdate,
    FounderGoalCreate,
    FounderOverrideCreate,
    FounderPredictionCreate,
    FounderPredictionScore,
    FounderTaskCreate,
    IdentityCharterUpsert,
    IngestFileRequest,
    IngestFolderRequest,
    IngestTextRequest,
    InitiativeCreate,
    KillCriteriaCreate,
    MemoryCreate,
    MemorySearch,
    MissedCallReviewCreate,
    ModelBenchmarkRequest,
    NotificationChannelUpdate,
    NotifyRequest,
    ObjectLinkCreate,
    ReflectionCreate,
    RelationshipCharterUpsert,
    RuntimeContextRequest,
    RuntimeLoopRequest,
    RuntimeRespondRequest,
    SemanticSearchRequest,
    SkillRouteRequest,
    SkillScanRequest,
    StrategyEntryCreate,
    SurfacingBriefRequest,
    StrategyObjectCreate,
    ThesisConflictCreate,
    TradingBotAdvisoryGenerateRequest,
    TradingBotAdvisoryScoreRequest,
    TradingBotDailyBriefRequest,
    TradingBotEvidenceSnapshotRequest,
    TradingBotJudgmentScoreRequest,
    TradingBotOpsCheckRequest,
    TradingBotRecommendationCreate,
    TradingBotSQLiteQueryRequest,
    TradingBotTrainingRunRequest,
    TradingBotTriggerProposalRequest,
    VoiceCharterUpsert,
    VoiceConverseRequest,
    VoiceSpeakRequest,
    VoiceTranscribeRequest,
    WorkItemCreate,
    WorkRunRequest,
    WorkScanRequest,
)
from .ollama import OllamaClient, OllamaError
from .ops import KernelOpsService
from .prompts import PromptProfileRegistry
from .actions import ActionPipelineService
from .commitments import CommitmentLedger
from .notify import NotificationBus
from .runtime import (
    RuntimeService,
    _charter_personality_contract,
    _format_identity_charter_for_prompt,
    _format_relationship_charters_for_prompt,
    _format_voice_charter_for_prompt,
)
from .skills import SkillService
from .surfacing import SurfacingService
from .voice import VoiceNotConfigured, VoiceService
from .teaching import DeepThoughtTeachingBridge
from .trading_bot import TradingBotBridge
from .tools import ToolRegistry
from .tray import TrayService
from .vault import VaultService


UI_DIR = Path(__file__).resolve().parents[2] / "ui"


_VAULT_APPROVAL_NOTE = (
    "Vault move/delete is a file mutation: approve and dispatch the work item with the typed "
    "confirmation phrase to run it. Deletes and clobbered targets go to a restorable trash."
)


def create_app(config: KernelConfig | None = None, *, run_boot_maintenance: bool = True) -> FastAPI:
    """Build the kernel app. ``run_boot_maintenance`` runs the serving-boot tiers
    (semantic reindex, memory-file mirror, and the abandoned-thread sweep). It is
    on for the real serve path and off for read-only introspection builds (e.g.
    the self-knowledge snapshot, which constructs a throwaway app only to
    enumerate handlers) — those must never mutate the DB, and the sweep ends
    conversations, so constructing an app for inspection must not trigger it."""
    cfg = config or load_config()
    ensure_local_paths(cfg)
    local_token = _resolve_local_token(cfg)
    _warn_on_weak_posture(cfg, local_token)

    db = KernelDatabase(cfg.paths.database_path)
    db.migrate()
    ollama = OllamaClient(cfg.ollama)
    authority = AuthorityPolicy.from_config(cfg)
    ingestion = IngestionService(config=cfg, db=db, embedder=ollama)
    # memory.write routes through the governed ingestion path (secret filter +
    # dedupe + embedding + mirror), not a raw insert.
    tools = ToolRegistry(db, authority=authority, ingestion=ingestion)
    founder = FounderService(config=cfg, db=db)
    skills = SkillService(config=cfg, db=db, embedder=ollama)
    handlers = ActionHandlerRegistry(db=db, config=cfg)
    trading_bot = TradingBotBridge(config=cfg, db=db, founder=founder)
    work_queue = WorkQueueService(
        config=cfg,
        db=db,
        authority=authority,
        ingestion=ingestion,
        inventory_provider=lambda: _inventory_payload(cfg, authority, tools, db, founder),
    )
    approvals = ApprovalService(
        db=db,
        handlers=handlers,
        authority=authority,
        typed_confirmation_phrase=authority.summary()["typed_confirmation_phrase"],
    )
    conversations = ConversationService(config=cfg, db=db, ollama=ollama, ingestion=ingestion)
    critic = ContrarianCritic(config=cfg, db=db, ollama=ollama, founder=founder)
    runtime = RuntimeService(
        config=cfg,
        db=db,
        authority=authority,
        founder=founder,
        ingestion=ingestion,
        work_queue=work_queue,
        ollama=ollama,
        skills=skills,
        conversations=conversations,
        critic=critic,
        trading_bot=trading_bot,
        approvals=approvals,
        inventory_provider=lambda: _inventory_payload(cfg, authority, tools, db, founder),
    )
    teaching = DeepThoughtTeachingBridge(config=cfg, db=db, founder=founder, ingestion=ingestion)
    experiments = ExperimentService(config=cfg, db=db, founder=founder, ingestion=ingestion)
    connectors = ConnectorService(config=cfg, db=db, founder=founder, ingestion=ingestion, work_queue=work_queue)
    handlers.register(
        "external.connector.sync",
        "Read-only sync of an approved external connector into staged candidate items.",
        connectors.sync_from_work_item,
    )
    browser = BrowserService(config=cfg, db=db, work_queue=work_queue)
    browser.register_into(handlers)
    vault = VaultService(config=cfg, db=db, work_queue=work_queue)
    vault.register_into(handlers)
    devtools = DevToolsHandlers(db=db, config=cfg)
    devtools.register_into(handlers)
    handlers.register(
        "external.dt_recommendation.ingest",
        "Append an observe-only Zade/DT advisory recommendation to the trading-bot dt_recommendations lane.",
        trading_bot.ingest_recommendation_from_work_item,
    )
    handlers.register(
        "external.dt_trigger.propose",
        "Record an approved dt_trigger proposal locally without running the trading bot.",
        trading_bot.record_dt_trigger_proposal_from_work_item,
    )
    ops = KernelOpsService(config=cfg, db=db, ollama=ollama, ui_dir=UI_DIR)
    evals = EvalService(config=cfg, db=db, ollama=ollama, runtime=runtime, critic=critic)
    voice = VoiceService(config=cfg, db=db, runtime=runtime)
    strategy_review = StrategyReviewService(
        config=cfg, db=db, founder=founder, ingestion=ingestion,
        typed_confirmation_phrase=authority.summary()["typed_confirmation_phrase"],
    )
    channel_auth = ChannelAuth(db)
    bus = NotificationBus(db=db, voice=voice)
    commitments = CommitmentLedger(db=db, bus=bus)
    actions = ActionPipelineService(db=db, authority=authority, founder=founder, work_queue=work_queue, bus=bus)
    surfacing = SurfacingService(config=cfg, db=db, ollama=ollama, bus=bus)
    tray = TrayService(config=cfg, db=db, bus=bus, ollama=ollama)
    research = ResearchService(
        config=cfg, db=db, founder=founder, ingestion=ingestion, work_queue=work_queue, bus=bus
    )
    research.register_into(handlers)
    # Let the chat runtime route founder research commands into the gated research
    # queue. Injected here because ResearchService is built after the runtime (it
    # needs the notification bus, which is built later).
    runtime.research = research

    # Specialist swarm (hybrid). Roles run LOCALLY (no approval); delegation hands
    # heavy work OUT to an external agent as an approval-gated L3 action (auto-invoke
    # bounded by a daily budget). Screen awareness is a local, on-demand read.
    roles = RolePassService(config=cfg, db=db, ollama=ollama)
    # Local model inventory + native coding agent: the default delegated-build
    # engine. Everything model-shaped runs on the loopback Ollama client above.
    inventory = ModelInventoryService(config=cfg, ollama=ollama)

    def command_audit(payload: dict[str, object]) -> None:
        db.audit(
            actor="build.command_runner",
            action=f"build.command.{payload.get('event') or 'event'}",
            target=str(payload.get("workspace") or ""),
            permission_tier="L2_FILE_WRITE",
            status="ok" if payload.get("ok", True) else "failed",
            details=dict(payload),
        )

    command_runner = GovernedCommandRunner(
        policies=coding_agent_command_policies(),
        artifact_root=cfg.paths.data_dir / "build-command-runs",
        audit=command_audit,
    )
    toolchains = ToolchainRegistry()
    coding_agent = CodingAgentService(
        config=cfg,
        db=db,
        ollama=ollama,
        inventory=inventory,
        notifier=bus,
        command_runner=command_runner,
    )
    build_store = BuildStore(db)
    build_budget = BuildBudgetService(
        build_store,
        cfg.build.anthropic_pricing.snapshot(),
        warning_percent=cfg.build.warning_percent,
    )
    build_router = BuildRouter(
        lease_lookup=build_store.get_active_lease,
        cloud_enabled=(
            cfg.build.enabled
            and cfg.anthropic.enabled
            and cfg.ollama.provider_policy != "local_only"
        ),
        pricing_current=cfg.build.anthropic_pricing.is_current,
    )
    anthropic_build_transport = AnthropicClient(
        cfg.anthropic, provider_policy=cfg.ollama.provider_policy
    )

    def build_cloud_agent_factory(session_id: int, authorize_egress: Any) -> CodingAgentService:
        model_client = AnthropicBuildModelClient(
            session_id=session_id,
            budget=build_budget,
            sdk_client=anthropic_build_transport.sdk_client(),
            authorize_egress=authorize_egress,
            provider_overhead_tokens=cfg.build.provider_overhead_tokens,
        )
        return CodingAgentService(
            config=cfg,
            db=db,
            ollama=ollama,
            model_client=model_client,
            inventory=inventory,
            notifier=bus,
            command_runner=command_runner,
        )

    build_service = BuildService(
        config=cfg,
        db=db,
        assessor=BuildAssessmentService(
            local_client=ollama,
            workspace_policy=BuildWorkspacePolicy(cfg.delegation.workspace_root),
        ),
        store=build_store,
        budget=build_budget,
        router=build_router,
        local_coding_agent=coding_agent,
        cloud_coding_agent_factory=build_cloud_agent_factory,
        egress_policy=EgressPolicy.from_config(cfg),
        typed_confirmation_phrase=authority.summary()["typed_confirmation_phrase"],
    )

    def browser_capture(*, url: str, workspace: Path) -> dict[str, Any]:
        del workspace
        evidence_dir = (
            cfg.paths.hot_root
            / "Zade"
            / "build-browser-evidence"
            / uuid4().hex
        )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        return browser.run_verification_flow(
            steps=[
                {"type": "navigate", "url": url},
                {"type": "read", "selector": "body"},
                {
                    "type": "screenshot",
                    "path": str(evidence_dir / "page.png"),
                    "full_page": True,
                },
            ],
            trace_path=str(evidence_dir / "trace.zip"),
        )

    verification = BuildVerificationService(
        toolchains=toolchains,
        runner=command_runner,
        store=build_store,
        browser_capture=browser_capture,
    )

    def verification_executor(task: Any, assessment: Any) -> dict[str, Any]:
        current = build_store.get_task(task.id)
        report = verification.verify(
            assessment.workspace,
            session_id=task.session_id,
            task_id=task.id,
            run_id=current.active_run_id if current else None,
            profile_id=str(task.payload.get("toolchain_profile") or "") or None,
            browser_url=str(task.payload.get("browser_url") or ""),
            android_device=str(task.payload.get("android_device") or ""),
        )
        return asdict(report) | {
            "status": "passed" if report.ok else "blocked" if report.blocked else "failed"
        }

    def github_ci_factory(
        workspace: str | Path,
        authorize_write: Any | None = None,
    ) -> GitHubCIClient:
        return GitHubCIClient(
            runner=command_runner,
            workspace=workspace,
            authorize_write=authorize_write,
        )

    def github_executor(task: Any, assessment: Any) -> dict[str, Any]:
        return github_ci_factory(assessment.workspace).execute_build_task(task, assessment)

    planner = BuildPlanner(
        store=build_store,
        toolchains=toolchains,
        ios_workflow=cfg.build.ios_workflow,
    )

    def cancel_build_commands(session_id: int) -> None:
        session = build_store.get_session(session_id)
        if session is not None:
            command_runner.cancel_workspace(session.workspace)

    orchestrator = BuildOrchestrator(
        store=build_store,
        planner=planner,
        router=build_router,
        local_agent=coding_agent,
        cloud_executor=build_service.execute_cloud_task,
        verification_executor=verification_executor,
        github_executor=github_executor,
        cancellation_callback=cancel_build_commands,
    )
    execution_manager = BuildExecutionManager(
        store=build_store,
        orchestrator=orchestrator,
        max_workers=cfg.build.max_workers,
    )
    build_service.configure_orchestration(
        orchestrator=orchestrator,
        execution_manager=execution_manager,
    )
    openai_budget = BuildBudgetService(
        build_store,
        cfg.openai_review.pricing.snapshot(),
        warning_percent=cfg.build.warning_percent,
    )
    calibration = BuildCalibrationService(build_store)
    managed_agents = ManagedAgentsReadinessService(build_store)

    def openai_review_client(authorize_egress: Any) -> OpenAIReviewClient:
        return OpenAIReviewClient(
            config=cfg.openai_review,
            budget=openai_budget,
            authorize_egress=authorize_egress,
        )
    delegation = DelegationService(
        config=cfg,
        db=db,
        founder=founder,
        work_queue=work_queue,
        coding_agent=coding_agent,
        build_service=build_service,
    )
    delegation.register_into(handlers)
    # Let the chat runtime route founder build commands ("build this out for me")
    # into a gated delegation brief instead of a text-only architecture outline.
    runtime.delegation = delegation
    screen = ScreenService(config=cfg, db=db)

    # Serving-boot maintenance. Skipped for read-only introspection builds so
    # merely constructing an app never touches (let alone mutates) the DB.
    if run_boot_maintenance:
        # Tier 4: keep the semantic memory index current. Incremental + best-effort —
        # a fresh DB or an embedder outage is a harmless no-op, and unchanged memories
        # are skipped, so steady-state startup makes no embedder calls.
        try:
            ingestion.rebuild_memory_embeddings()
        except Exception:
            pass
        # Tier 6: mirror memories to human-editable files (their source of truth).
        # Idempotent backfill — only writes files that don't exist yet.
        try:
            ingestion.export_memories_to_files()
        except Exception:
            pass
        # Document/chunk semantic index with retrieval prefixes. Incremental +
        # best-effort — unchanged chunks are skipped, so steady-state startup is a no-op.
        try:
            ingestion.rebuild_chunk_embeddings()
        except Exception:
            pass
        # Skill-routing embeddings, same contract: hash-guarded (content AND
        # embed-recipe version), so steady-state startup makes no embedder calls,
        # and a recipe bump re-embeds the library once at next boot.
        try:
            skills.rebuild_embeddings()
        except Exception:
            pass
        # Tier 8: finalize abandoned threads. The UI resumes only the most-recent
        # active conversation; every older active thread is unreachable and would
        # never distill on its own (short threads never hit the auto-distill
        # threshold, and 'New Thread' is the only other trigger). Promote their
        # durable knowledge now. Best-effort + loss-safe — a distill failure leaves
        # the thread active to retry next boot rather than stranding its turns.
        try:
            conversations.sweep_abandoned()
        except Exception:
            pass
        try:
            execution_manager.recover()
        except Exception:
            log.exception("Durable build recovery failed during startup")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        execution_manager.shutdown(wait=False)
        command_runner.cancel_all()

    app = FastAPI(
        title=f"{cfg.identity.name} Local AI Co-founder Kernel",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
    app.state.config = cfg
    app.state.local_token = local_token
    app.state.db = db
    app.state.ollama = ollama
    app.state.authority = authority
    app.state.tools = tools
    app.state.ingestion = ingestion
    app.state.founder = founder
    app.state.skills = skills
    app.state.handlers = handlers
    app.state.trading_bot = trading_bot
    app.state.work_queue = work_queue
    app.state.approvals = approvals
    app.state.conversations = conversations
    app.state.critic = critic
    app.state.runtime = runtime
    app.state.teaching = teaching
    app.state.experiments = experiments
    app.state.connectors = connectors
    app.state.browser = browser
    app.state.vault = vault
    app.state.tray = tray
    app.state.research = research
    app.state.roles = roles
    app.state.delegation = delegation
    app.state.build = build_service
    app.state.build_store = build_store
    app.state.build_budget = build_budget
    app.state.build_router = build_router
    app.state.command_runner = command_runner
    app.state.toolchains = toolchains
    app.state.build_verification = verification
    app.state.build_orchestrator = orchestrator
    app.state.build_execution_manager = execution_manager
    app.state.github_ci_factory = github_ci_factory
    app.state.openai_budget = openai_budget
    app.state.openai_review_client = openai_review_client
    app.state.build_calibration = calibration
    app.state.managed_agents = managed_agents
    app.state.anthropic_build_transport = anthropic_build_transport
    app.state.screen = screen
    app.state.surfacing = surfacing
    app.state.bus = bus
    app.state.commitments = commitments
    app.state.actions = actions
    app.state.ops = ops
    app.state.evals = evals
    app.state.voice = voice

    @app.middleware("http")
    async def local_mutation_guard(request: Request, call_next):
        if _mutation_requires_token(cfg, request, local_token):
            supplied = request.headers.get("x-zade-token", "")
            if not hmac.compare_digest(supplied, local_token):
                response: Any = JSONResponse(
                    status_code=401,
                    content={
                        "detail": "Local mutation token required.",
                        "hint": "The /ui pages auto-load it from /session/token; or send X-Zade-Token.",
                    },
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        # Stamp local-first security headers on every response (including the 401
        # above), so a strict CSP blocks any external load/exfil from the browser.
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        # StaticFiles emits ETag/Last-Modified but no Cache-Control, so WebView2
        # (and the Claude browser pane) fall back to heuristic caching and serve
        # stale ui/zade-ui.js|css after edits. Force revalidation on every /ui
        # asset — the existing ETag still yields cheap 304s when unchanged.
        if request.url.path.startswith("/ui"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # Added last so CORS wraps the mutation guard: preflight OPTIONS is answered
    # before the guard runs, and the guard's 401 gets CORS headers stamped — so
    # the dev browser can read the "token required" hint instead of an opaque
    # CORS failure. No-op unless [security] cors_dev_origins is configured.
    _configure_dev_cors(app, cfg)

    @app.get("/health")
    def health() -> dict[str, Any]:
        ollama_status: dict[str, Any]
        try:
            ollama_status = {"ok": True, "details": ollama.health()}
        except OllamaError as exc:
            ollama_status = {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "name": cfg.identity.name,
            "local_only": True,
            "uptime_seconds": ops.uptime_seconds(),
            "database": str(cfg.paths.database_path),
            "hot_root": str(cfg.paths.hot_root),
            "cold_root": str(cfg.paths.cold_root),
            "inbox": str(cfg.paths.inbox_dir),
            "cold_raw_ingest": str(cfg.paths.cold_raw_ingest_dir),
            "ollama": ollama_status,
            "model_roles": cfg.ollama.roles(),
            "prompt_profiles": {
                "default": runtime.default_prompt_profile_id(),
                "available": [item["id"] for item in runtime.available_prompt_profiles()],
            },
            "work_queue": db.work_queue_counts(),
            "authority": {
                "policy_version": authority.summary()["policy_version"],
                "typed_confirmation_phrase": authority.summary()["typed_confirmation_phrase"],
            },
            "security": _security_summary(cfg, local_token),
            "skills": db.skill_summary(),
            "tools": tools.list_tools(),
            # An enabled channel that is not running is a health problem, not a
            # footnote — this is what let the Telegram outage go silent.
            "channels": {
                "telegram": {
                    "enabled": cfg.telegram.enabled,
                    "running": telegram_adapter.running,
                    "ok": (not cfg.telegram.enabled) or telegram_adapter.running,
                },
            },
        }

    @app.get("/models")
    def models() -> dict[str, Any]:
        try:
            installed = ollama.tags()
        except OllamaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        installed_names = {item.get("name") for item in installed.get("models", [])}
        roles = cfg.ollama.roles()
        return {
            "roles": roles,
            "installed": installed.get("models", []),
            "missing_roles": {
                role: model for role, model in roles.items() if not _model_is_installed(model, installed_names)
            },
        }

    @app.get("/models/inventory")
    def models_inventory(probe: bool = False) -> dict[str, Any]:
        """Installed local models with details, capabilities, and (optionally)
        the live native tool-call probe. Never pulls a model."""
        try:
            return {"models": inventory.snapshot(probe=probe)}
        except OllamaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/providers/status")
    def providers_status() -> dict[str, Any]:
        """The provider-policy truth surface: policy, endpoint, per-role models,
        cloud posture, bridge mode, and the most recent model call. LOCAL means
        loopback Ollama with a verified-local model; anything else says so."""
        provider = ollama.provider_info()
        roles = cfg.ollama.roles()
        coding_agent_status: dict[str, Any] = {"model": "", "error": ""}
        try:
            coding_agent_status["model"] = inventory.resolve_coding_agent_model()
        except OllamaError as exc:
            coding_agent_status["error"] = str(exc)
        installed: list[str] = []
        inventory_error = ""
        try:
            installed = inventory.installed()
        except OllamaError as exc:
            inventory_error = str(exc)
        last_calls = db.list_model_calls(limit=1)
        last_call = last_calls[0].__dict__ if last_calls else None
        engine = getattr(cfg.delegation, "engine", "native")
        return {
            "indicator": "LOCAL" if provider["verified_local"] else "CLOUD",
            "provider_policy": provider["provider_policy"],
            "provider": provider,
            "ollama_host": cfg.ollama.base_url,
            "models_by_role": roles | {"coding_agent": coding_agent_status["model"]},
            "coding_agent": coding_agent_status,
            "ollama_cloud_disabled": inventory.ollama_cloud_disabled(),
            "delegation_engine": engine,
            "claude_code_bridge_active": engine == "bridge",
            "installed_models": installed,
            "inventory_error": inventory_error,
            "last_model_call": last_call,
        }

    @app.get("/models/telemetry")
    def model_telemetry(limit: int = 250) -> dict[str, Any]:
        return db.model_call_summary(limit=limit)

    @app.get("/models/telemetry/calls")
    def model_telemetry_calls(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": [call.__dict__ for call in db.list_model_calls(status=status, limit=limit)]}

    @app.get("/skills/summary")
    def skills_summary() -> dict[str, Any]:
        return db.skill_summary() | {"source_dir": str(cfg.skills.source_dir), "lock_file": str(cfg.skills.lock_file)}

    @app.get("/skills")
    def list_skills(
        enabled: bool | None = None,
        risk_tier: str | None = None,
        source: str | None = None,
        limit: int = 250,
    ) -> dict[str, Any]:
        return skills.list_skills(enabled=enabled, risk_tier=risk_tier, source=source, limit=limit)

    @app.post("/skills/scan")
    def scan_skills(payload: SkillScanRequest | None = None) -> dict[str, Any]:
        request = payload or SkillScanRequest()
        try:
            return skills.scan(source_dir=request.source_dir, enable_defaults=request.enable_defaults)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/skills/route")
    def route_skills(payload: SkillRouteRequest) -> dict[str, Any]:
        routed = skills.route(query=payload.query, task_type=payload.task_type, limit=payload.limit)
        audit_id = db.audit(
            actor="skills",
            action="skills.route",
            target=payload.query[:240],
            permission_tier="L0_READ",
            status="ok",
            details=routed.summary(),
        )
        return routed.summary() | {"audit_id": audit_id}

    @app.get("/skills/invocations")
    def skill_invocations(limit: int = 25) -> dict[str, Any]:
        return {"items": db.recent_skill_invocations(limit=limit)}

    @app.get("/skills/{name}")
    def get_skill(name: str) -> dict[str, Any]:
        try:
            return {"item": skills.get_skill(name)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/skills/{name}/enable")
    def enable_skill(name: str) -> dict[str, Any]:
        try:
            return {"item": skills.enable(name)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/skills/{name}/disable")
    def disable_skill(name: str) -> dict[str, Any]:
        try:
            return {"item": skills.disable(name)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/models/benchmark")
    def model_benchmark(payload: ModelBenchmarkRequest | None = None) -> dict[str, Any]:
        request = payload or ModelBenchmarkRequest()
        return ops.benchmark_models(**request.model_dump())

    @app.get("/ops/health-check")
    def ops_health_check(max_cadence_age_hours: int = 30, require_recent_cadence: bool = False) -> dict[str, Any]:
        return ops.health_check(
            max_cadence_age_hours=max_cadence_age_hours,
            require_recent_cadence=require_recent_cadence,
        )

    @app.get("/ops/security")
    def ops_security() -> dict[str, Any]:
        return _security_summary(cfg, local_token)

    @app.get("/ops/providers")
    def ops_providers() -> dict[str, Any]:
        """At-a-glance provider readiness across the local model provider and the
        cloud strategic-review path. Cloud is OFF unless the founder has enabled
        [anthropic] AND set an API key AND raised provider_policy above local_only.
        Booleans only — no secret is exposed (key presence is a bool)."""
        local = ollama.provider_info()
        anthropic = strategy_review.readiness()
        return {
            "provider_policy": local.get("provider_policy"),
            "local_only": local.get("provider_policy") == "local_only",
            "local": local,
            "cloud": {"anthropic": anthropic},
            "cloud_ready": bool(anthropic.get("ready")),
        }

    @app.get("/session/token")
    def session_token() -> dict[str, Any]:
        """Hand the loopback UI its mutation token so it can bootstrap without a
        manual paste. Only served on a loopback bind — a networked bind must not
        surrender the token to remote clients (and same-origin policy already
        stops a cross-site page from reading this response)."""
        if not _host_is_loopback(cfg.app.host):
            raise HTTPException(status_code=403, detail="Token bootstrap is disabled on a non-loopback bind.")
        return {
            "token": local_token,
            "header": "X-Zade-Token",
            "storage": "zadeKernelToken",
            "required": bool(local_token and cfg.security.protect_mutations),
        }

    @app.get("/ops/supervision")
    def ops_supervision(limit: int = 50) -> dict[str, Any]:
        return ops.supervision(limit=limit)

    @app.post("/ops/backup")
    def ops_backup(payload: BackupCreateRequest | None = None) -> dict[str, Any]:
        request = payload or BackupCreateRequest()
        return ops.create_backup(label=request.label)

    @app.get("/ops/backups")
    def ops_backups(limit: int = 25) -> dict[str, Any]:
        return {"items": ops.list_backups(limit=limit)}

    @app.post("/ops/backups/prune")
    def ops_prune_backups(payload: BackupRetentionRequest | None = None) -> dict[str, Any]:
        request = payload or BackupRetentionRequest()
        return ops.prune_backups(**request.model_dump())

    @app.get("/tools")
    def list_tools() -> list[dict[str, Any]]:
        return tools.list_tools()

    @app.get("/authority")
    def get_authority() -> dict[str, Any]:
        return authority.summary()

    @app.post("/authority/evaluate")
    def evaluate_authority(payload: AuthorityEvaluateRequest) -> dict[str, Any]:
        result = authority.evaluate(
            AuthorityRequest(
                action=payload.action,
                permission_tier=payload.permission_tier,
                target=payload.target,
                metadata=payload.metadata,
            )
        )
        audit_id = db.audit(
            actor="api",
            action="authority.evaluate",
            target=payload.target or payload.action,
            permission_tier=payload.permission_tier,
            status=result.decision.value,
            details={"request": payload.model_dump(), "result": result.as_dict()},
        )
        return result.as_dict() | {"audit_id": audit_id}

    @app.get("/self-inventory")
    def self_inventory() -> dict[str, Any]:
        return _inventory_payload(cfg, authority, tools, db, founder)

    @app.get("/identity/charter")
    def get_identity_charter() -> dict[str, Any]:
        return {"charter": founder.get_identity_charter()}

    @app.post("/identity/charter")
    def upsert_identity_charter(payload: IdentityCharterUpsert) -> dict[str, Any]:
        return {"charter": founder.upsert_identity_charter(payload.model_dump())}

    @app.get("/identity/relationships")
    def list_relationship_charters(status: str | None = None, limit: int = 25) -> dict[str, Any]:
        return {"charters": founder.list_relationship_charters(status=status, limit=limit)}

    @app.get("/identity/relationships/{subject_name}")
    def get_relationship_charter(subject_name: str, relationship_type: str = "protected_principal") -> dict[str, Any]:
        return {"charter": founder.get_relationship_charter(subject_name, relationship_type)}

    @app.post("/identity/relationships")
    def upsert_relationship_charter(payload: RelationshipCharterUpsert) -> dict[str, Any]:
        return {"charter": founder.upsert_relationship_charter(payload.model_dump())}

    @app.get("/identity/voice")
    def get_voice_charter() -> dict[str, Any]:
        return {"charter": founder.get_voice_charter()}

    @app.post("/identity/voice")
    def upsert_voice_charter(payload: VoiceCharterUpsert) -> dict[str, Any]:
        return {"charter": founder.upsert_voice_charter(payload.model_dump())}

    @app.get("/runtime/charter-stack")
    def runtime_charter_stack() -> dict[str, Any]:
        return runtime.charter_stack()

    @app.get("/runtime/profiles")
    def runtime_profiles() -> dict[str, Any]:
        return {
            "default_profile": runtime.default_prompt_profile_id(),
            "profiles": runtime.available_prompt_profiles(),
            "precedence": ["request.profile", "conversation.metadata.prompt_profile", "config.prompt_profiles.default"],
        }

    @app.get("/runtime/context")
    def runtime_context(
        message: str = "",
        task_type: str = "general",
        profile: str | None = None,
        use_memory: bool = True,
        use_semantic_memory: bool = True,
        semantic_limit: int = 4,
        use_skills: bool = True,
        skill_limit: int = 3,
    ) -> dict[str, Any]:
        try:
            return runtime.context(
                message=message,
                task_type=task_type,  # type: ignore[arg-type]
                profile=profile,
                use_memory=use_memory,
                use_semantic_memory=use_semantic_memory,
                semantic_limit=semantic_limit,
                use_skills=use_skills,
                skill_limit=skill_limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/runtime/context")
    def runtime_context_post(payload: RuntimeContextRequest) -> dict[str, Any]:
        try:
            return runtime.context(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/runtime/respond")
    def runtime_respond(payload: RuntimeRespondRequest) -> dict[str, Any]:
        try:
            return runtime.respond(**payload.model_dump())
        except OllamaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/memory/reindex")
    def memory_reindex() -> dict[str, Any]:
        """Rebuild the derived semantic memory index from the memories table.
        Incremental (skips unchanged memories); safe to call any time."""
        return ingestion.rebuild_memory_embeddings()

    @app.post("/memory/export-files")
    def memory_export_files() -> dict[str, Any]:
        """Backfill the human-editable memory file store from the DB (idempotent)."""
        return ingestion.export_memories_to_files()

    @app.post("/memory/rebuild-from-files")
    def memory_rebuild_from_files() -> dict[str, Any]:
        """Rebuild the DB index to exactly match the memory files (files are the
        source of truth). Use after hand-editing or deleting memory files."""
        return ingestion.rebuild_index_from_files()

    @app.post("/memory/reindex-documents")
    def memory_reindex_documents() -> dict[str, Any]:
        """Re-embed ingested document chunks with retrieval prefixes (incremental)."""
        return ingestion.rebuild_chunk_embeddings()

    @app.post("/conversations")
    def create_conversation(payload: ConversationCreate | None = None) -> dict[str, Any]:
        request = payload or ConversationCreate()
        return {"conversation": conversations.create(title=request.title, metadata=request.metadata)}

    @app.get("/conversations")
    def list_conversations(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"conversations": conversations.list(status=status, limit=limit)}

    @app.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: int, turn_limit: int = 50) -> dict[str, Any]:
        try:
            return {"conversation": conversations.get(conversation_id, turn_limit=turn_limit)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/conversations/{conversation_id}/turns")
    def get_conversation_turns(conversation_id: int, limit: int = 100) -> dict[str, Any]:
        try:
            return {"turns": conversations.list_turns(conversation_id, limit=limit)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/conversations/{conversation_id}/distill")
    def distill_conversation(conversation_id: int) -> dict[str, Any]:
        """Promote durable knowledge from this thread's not-yet-distilled turns into
        searchable memory, on demand (the runtime also does this automatically as
        turns age out of the recent window)."""
        try:
            result = conversations.distill(conversation_id, min_turns=1, only_aged_out=False)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"result": result or {"status": "nothing_to_distill", "written": [], "count": 0}}

    @app.post("/conversations/{conversation_id}/end")
    def end_conversation(conversation_id: int) -> dict[str, Any]:
        """Close a thread: final distill + mark it ended so a later boot starts a
        fresh session instead of piling onto it."""
        try:
            return conversations.end_session(conversation_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/surface/attention")
    def surface_attention() -> dict[str, Any]:
        return surfacing.scan()

    @app.post("/surface/brief")
    def surface_brief(payload: SurfacingBriefRequest | None = None) -> dict[str, Any]:
        request = payload or SurfacingBriefRequest()
        return surfacing.brief(narrate=request.narrate, force=request.force)

    @app.get("/evals/cases")
    def list_eval_cases(category: str | None = None, enabled: bool | None = None) -> dict[str, Any]:
        evals.ensure_default_cases()
        return {"items": evals.list_cases(category=category, enabled=enabled)}

    @app.post("/evals/cases")
    def upsert_eval_case(payload: EvalCaseUpsert) -> dict[str, Any]:
        try:
            return {"item": evals.upsert_case(payload.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/evals/run")
    def run_evals(payload: EvalRunRequest | None = None) -> dict[str, Any]:
        request = payload or EvalRunRequest()
        return evals.run(
            label=request.label,
            categories=request.categories or None,
            case_names=request.case_names or None,
            max_cases=request.max_cases,
        )

    @app.get("/evals/runs")
    def list_eval_runs(limit: int = 25) -> dict[str, Any]:
        return {"items": evals.list_runs(limit=limit)}

    @app.get("/evals/runs/{run_id}")
    def get_eval_run(run_id: int) -> dict[str, Any]:
        try:
            return {"item": evals.get_run(run_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/connectors")
    def list_connectors(enabled: bool | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": connectors.list_connectors(enabled=enabled, limit=limit)}

    @app.post("/connectors")
    def create_connector(payload: ConnectorCreate) -> dict[str, Any]:
        try:
            return {"item": connectors.create_connector(payload.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/connectors/items")
    def list_connector_items(
        status: str | None = None,
        connector: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {"items": connectors.list_items(status=status, connector=connector, limit=limit)}

    @app.post("/connectors/items/import")
    def import_connector_items(payload: ConnectorItemsImport) -> dict[str, Any]:
        try:
            return connectors.import_items(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/connectors/items/{item_id}/dismiss")
    def dismiss_connector_item(item_id: int, payload: ConnectorItemDismiss | None = None) -> dict[str, Any]:
        request = payload or ConnectorItemDismiss()
        try:
            return {"item": connectors.dismiss_item(item_id, reason=request.reason)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/connectors/{name}")
    def get_connector(name: str) -> dict[str, Any]:
        try:
            return {"item": connectors.get_connector(name)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/connectors/{name}/oauth/begin")
    def begin_connector_oauth(name: str) -> dict[str, Any]:
        """Start Microsoft device-code enrollment for an xoauth2 IMAP connector.
        The founder types the returned user_code at the verification URI; the
        kernel polls in the background and caches tokens on success."""
        try:
            return {"flow": connectors.begin_oauth(name)}
        except ValueError as exc:
            status = 404 if "not found" in str(exc).lower() else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get("/connectors/{name}/oauth/status")
    def connector_oauth_status(name: str) -> dict[str, Any]:
        try:
            return connectors.oauth_status(name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/connectors/{name}/update")
    def update_connector(name: str, payload: ConnectorUpdate) -> dict[str, Any]:
        try:
            return {"item": connectors.update_connector(name, payload.model_dump(exclude_unset=True))}
        except ValueError as exc:
            status = 404 if "not found" in str(exc).lower() else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.post("/connectors/{name}/sync")
    def queue_connector_sync(name: str) -> dict[str, Any]:
        try:
            result = connectors.queue_sync(name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return result | {
            "note": (
                "Connector sync is an external action: approve and dispatch the work item "
                "with the typed confirmation phrase to run it."
            ),
        }

    @app.get("/browser/status")
    def browser_status() -> dict[str, Any]:
        return browser.status()

    @app.post("/browser/run")
    def browser_run(payload: BrowserRunRequest) -> dict[str, Any]:
        try:
            result = browser.queue_run(
                steps=payload.steps,
                title=payload.title,
                session_label=payload.session_label,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result | {
            "note": (
                "Headed browser automation is an external action: approve and dispatch the work item "
                "with the typed confirmation phrase to run it."
            ),
        }

    @app.get("/vault/status")
    def vault_status() -> dict[str, Any]:
        return vault.status()

    @app.get("/vault/list")
    def vault_list(path: str = "", limit: int | None = None) -> dict[str, Any]:
        try:
            return vault.list_entries(path, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/vault/search")
    def vault_search(query: str, path: str = "", limit: int | None = None) -> dict[str, Any]:
        try:
            return vault.search(query, path=path, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/vault/trash")
    def vault_trash(limit: int = 50) -> dict[str, Any]:
        return vault.list_trash(limit=limit)

    @app.post("/vault/move")
    def vault_move(payload: VaultMoveRequest) -> dict[str, Any]:
        try:
            if payload.dry_run:
                return vault.plan_move(
                    payload.src, payload.dst, allow_top_level=payload.allow_top_level, overwrite=payload.overwrite
                )
            result = vault.queue_move(
                payload.src, payload.dst, allow_top_level=payload.allow_top_level, overwrite=payload.overwrite
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result | {"note": _VAULT_APPROVAL_NOTE}

    @app.post("/vault/delete")
    def vault_delete(payload: VaultDeleteRequest) -> dict[str, Any]:
        try:
            if payload.dry_run:
                return vault.plan_delete(payload.path, allow_top_level=payload.allow_top_level)
            result = vault.queue_delete(payload.path, allow_top_level=payload.allow_top_level)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result | {"note": _VAULT_APPROVAL_NOTE}

    @app.post("/vault/restore")
    def vault_restore(payload: VaultRestoreRequest) -> dict[str, Any]:
        try:
            return vault.restore(payload.trash_id, overwrite=payload.overwrite)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/tray/state")
    def tray_state() -> dict[str, Any]:
        return tray.state()

    @app.get("/research/status")
    def research_status() -> dict[str, Any]:
        return research.status()

    @app.get("/research/topics")
    def research_topics(limit: int = 5) -> dict[str, Any]:
        return {"topics": research.derive_topics(limit=limit)}

    @app.post("/research/daydream")
    def research_daydream(payload: ResearchDaydreamRequest | None = None) -> dict[str, Any]:
        request = payload or ResearchDaydreamRequest()
        return research.daydream(limit=request.limit, notify=request.notify)

    @app.post("/research/run")
    def research_run(payload: ResearchRunRequest) -> dict[str, Any]:
        try:
            result = research.queue_research(
                topic=payload.topic, urls=payload.urls, create_evidence=payload.create_evidence
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result | {
            "note": (
                "Web research is an external action: approve and dispatch the work item with the typed "
                "confirmation phrase to fetch the sources."
            ),
        }

    # ---- Swarm: local roles ----
    @app.get("/roles")
    def list_roles() -> dict[str, Any]:
        return {"roles": roles.list_roles()}

    @app.get("/roles/status")
    def roles_status() -> dict[str, Any]:
        return roles.status()

    @app.post("/roles/run")
    def roles_run(payload: RolePassRequest) -> dict[str, Any]:
        try:
            return roles.run(role=payload.role, content=payload.content, subject=payload.subject)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- Swarm: delegation ----
    @app.get("/delegation/status")
    def delegation_status() -> dict[str, Any]:
        recent = build_service.list_sessions(limit=5)
        return delegation.status() | {
            "build": {
                "enabled": cfg.build.enabled,
                "provider": "anthropic",
                "model": cfg.build.anthropic_pricing.model,
                "pricing_current": cfg.build.anthropic_pricing.is_current(),
                "active_session_count": build_store.count_sessions(status="active"),
                "recent_sessions": recent,
            }
        }

    @app.post("/delegation/brief")
    def delegation_brief(payload: DelegationBriefRequest) -> dict[str, Any]:
        try:
            brief = delegation.build_brief(
                task=payload.task, context=payload.context, acceptance=payload.acceptance
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"brief": brief}

    @app.post("/delegation/run")
    def delegation_run(payload: DelegationQueueRequest) -> dict[str, Any]:
        try:
            return delegation.queue_delegation(
                task=payload.task,
                brief=payload.brief,
                context=payload.context,
                acceptance=payload.acceptance,
                auto_invoke=payload.auto_invoke,
                workspace=payload.workspace,
                directed=payload.directed,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- Governed build sessions ----
    def require_build_session(session_id: int) -> None:
        if build_store.get_session(session_id) is None:
            raise HTTPException(
                status_code=404, detail=f"Build session not found: {session_id}"
            )

    @app.post("/build/assess")
    def build_assess(payload: BuildAssessRequest) -> dict[str, Any]:
        if not cfg.build.enabled:
            raise HTTPException(status_code=503, detail="Build sessions are disabled.")
        try:
            return build_service.prepare(
                task=payload.task,
                workspace=payload.workspace,
                acceptance=payload.acceptance,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/build/sessions")
    def build_sessions(limit: int = 20) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
        return {"sessions": build_service.list_sessions(limit=limit)}

    @app.get("/build/sessions/{session_id}")
    def build_session_status(session_id: int) -> dict[str, Any]:
        require_build_session(session_id)
        return build_service.status(session_id)

    @app.post("/build/sessions/{session_id}/plan")
    def build_session_plan(
        session_id: int, payload: BuildPlanRequest | None = None
    ) -> dict[str, Any]:
        require_build_session(session_id)
        request = payload or BuildPlanRequest()
        try:
            orchestrator.ensure_plan(session_id, profile_id=request.profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return build_service.status(session_id)

    @app.get("/build/sessions/{session_id}/tasks")
    def build_session_tasks(session_id: int) -> dict[str, Any]:
        require_build_session(session_id)
        status = build_service.status(session_id)
        return {
            "session_id": session_id,
            "tasks": status["tasks"],
            "runs": status["task_runs"],
            "artifacts": status["artifacts"],
        }

    @app.post("/build/sessions/{session_id}/tasks")
    def build_session_task_create(
        session_id: int, payload: BuildTaskCreateRequest
    ) -> dict[str, Any]:
        require_build_session(session_id)
        try:
            task = build_store.create_task(
                session_id,
                phase=payload.phase,
                kind=BuildTaskKind(payload.kind),
                title=payload.title,
                payload={
                    "route": "local",
                    "instructions": payload.instructions,
                },
                dependencies=tuple(payload.dependencies),
                acceptance={"criteria": payload.acceptance},
                idempotency_key=payload.idempotency_key,
                max_attempts=payload.max_attempts,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"task": asdict(task)}

    @app.post("/build/sessions/{session_id}/tasks/{task_id}/retry")
    def build_session_task_retry(
        session_id: int, task_id: int, payload: BuildTaskRetryRequest
    ) -> dict[str, Any]:
        require_build_session(session_id)
        try:
            return build_service.retry_local_task(
                session_id, task_id, reason=payload.reason
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/build/sessions/{session_id}/approve")
    def build_session_approve(
        session_id: int, payload: BuildLeaseApproveRequest
    ) -> dict[str, Any]:
        require_build_session(session_id)
        try:
            result = build_service.approve(
                session_id,
                typed_phrase=payload.typed_confirmation,
                tier=payload.tier,
                audit_note=payload.audit_note,
            )
        except (AnthropicNotConfigured, AnthropicPolicyError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AnthropicError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _raise_build_provider_error(result)
        return result

    @app.post("/build/sessions/{session_id}/deny")
    def build_session_deny(
        session_id: int, payload: BuildLeaseDenyRequest | None = None
    ) -> dict[str, Any]:
        require_build_session(session_id)
        request = payload or BuildLeaseDenyRequest()
        try:
            return build_service.deny(
                session_id, note=request.note, resolved_by=request.resolved_by
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/build/sessions/{session_id}/run")
    def build_session_run(session_id: int) -> dict[str, Any]:
        require_build_session(session_id)
        try:
            result = build_service.run(session_id)
        except (AnthropicNotConfigured, AnthropicPolicyError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AnthropicError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        _raise_build_provider_error(result)
        return result

    @app.post("/build/sessions/{session_id}/run-next")
    def build_session_run_next(session_id: int) -> dict[str, Any]:
        return build_session_run(session_id)

    @app.post("/build/sessions/{session_id}/start")
    def build_session_start(session_id: int) -> dict[str, Any]:
        require_build_session(session_id)
        try:
            return build_service.start(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/build/sessions/{session_id}/pause")
    def build_session_pause(session_id: int) -> dict[str, Any]:
        require_build_session(session_id)
        try:
            return build_service.pause(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/build/sessions/{session_id}/resume")
    def build_session_resume(session_id: int) -> dict[str, Any]:
        require_build_session(session_id)
        try:
            return build_service.resume(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/build/sessions/{session_id}/cancel")
    def build_session_cancel(session_id: int) -> dict[str, Any]:
        require_build_session(session_id)
        try:
            return build_service.cancel(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/build/sessions/{session_id}/quarantine")
    def build_session_quarantine(
        session_id: int, payload: BuildQuarantineRequest
    ) -> dict[str, Any]:
        require_build_session(session_id)
        try:
            return build_service.quarantine(session_id, reason=payload.reason)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/build/runs/{run_id}")
    def build_run_status(run_id: int) -> dict[str, Any]:
        run = build_store.get_task_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Build run not found: {run_id}")
        return {"run": asdict(run)}

    @app.post("/build/runs/{run_id}/cancel")
    def build_run_cancel(run_id: int) -> dict[str, Any]:
        run = build_store.get_task_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Build run not found: {run_id}")
        try:
            return build_service.cancel(run.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/build/toolchains")
    def build_toolchains(workspace: str) -> dict[str, object]:
        try:
            return toolchains.inventory(workspace)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/build/sessions/{session_id}/verify")
    def build_session_verify(
        session_id: int, payload: BuildVerifyRequest | None = None
    ) -> dict[str, Any]:
        require_build_session(session_id)
        request = payload or BuildVerifyRequest()
        session = build_store.get_session(session_id)
        assert session is not None
        try:
            report = verification.verify(
                session.workspace,
                session_id=session_id,
                profile_id=request.profile_id,
                browser_url=request.browser_url,
                android_device=request.android_device,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(report)

    def github_authorizer(
        typed_confirmation: str,
    ) -> Any:
        def authorize(request: GitHubAuthorizationRequest) -> bool:
            allowed = hmac.compare_digest(
                typed_confirmation.strip(),
                authority.summary()["typed_confirmation_phrase"],
            )
            db.audit(
                actor="founder",
                action=f"github.{request.action}",
                target=request.workspace,
                permission_tier="L3_EXTERNAL_ACTION",
                status="approved" if allowed else "denied",
                details=request.details,
            )
            return allowed

        return authorize

    @app.get("/build/sessions/{session_id}/github/status")
    def build_github_status(session_id: int) -> dict[str, Any]:
        require_build_session(session_id)
        session = build_store.get_session(session_id)
        assert session is not None
        return github_ci_factory(session.workspace).status()

    @app.get("/build/sessions/{session_id}/github/runs")
    def build_github_runs(
        session_id: int, workflow: str = "", limit: int = 20
    ) -> dict[str, Any]:
        require_build_session(session_id)
        if not 1 <= limit <= 20:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 20")
        session = build_store.get_session(session_id)
        assert session is not None
        try:
            runs = github_ci_factory(session.workspace).list_runs(
                workflow=workflow, limit=limit
            )
        except GitHubCIError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"runs": [asdict(run) for run in runs]}

    @app.post("/build/sessions/{session_id}/github/dispatch")
    def build_github_dispatch(
        session_id: int, payload: GitHubWorkflowDispatchRequest
    ) -> dict[str, Any]:
        require_build_session(session_id)
        session = build_store.get_session(session_id)
        assert session is not None
        client = github_ci_factory(
            session.workspace,
            github_authorizer(payload.typed_confirmation),
        )
        try:
            return client.dispatch_workflow(
                payload.workflow, ref=payload.ref, inputs=payload.inputs
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (GitHubCIError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/build/sessions/{session_id}/github/runs/{run_id}/cancel")
    def build_github_run_cancel(
        session_id: int, run_id: int, payload: GitHubRunCancelRequest
    ) -> dict[str, Any]:
        require_build_session(session_id)
        session = build_store.get_session(session_id)
        assert session is not None
        try:
            return github_ci_factory(
                session.workspace,
                github_authorizer(payload.typed_confirmation),
            ).cancel_run(run_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (GitHubCIError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/build/sessions/{session_id}/review/status")
    def build_review_status(session_id: int) -> dict[str, Any]:
        require_build_session(session_id)
        status = openai_review_client(lambda _request: False).status()
        lease = build_store.get_active_lease(session_id, provider="openai")
        return status | {"lease": asdict(lease) if lease else None}

    @app.post("/build/sessions/{session_id}/review/prepare")
    def build_review_prepare(session_id: int) -> dict[str, Any]:
        require_build_session(session_id)
        status = openai_review_client(lambda _request: False).status()
        if not status["ready"]:
            raise HTTPException(status_code=503, detail={"blockers": status["blockers"]})
        existing = build_store.get_active_lease(session_id, provider="openai")
        if existing is not None:
            return {"status": "active", "lease": asdict(existing)}
        session = build_store.get_session(session_id)
        assert session is not None
        approval, _created = db.ensure_approval_request(
            source_type="openai_review_lease",
            source_id=session_id,
            title="Approve Small OpenAI build review lease",
            detail=f"Workspace: {session.workspace}\nModel: {cfg.openai_review.model}",
            action="build.review.lease.approve",
            target=session.workspace,
            permission_tier="L3_EXTERNAL_ACTION",
            authority_decision="approval_required",
            authority={"requires_typed_phrase": True},
            requested_by="build.review",
            metadata={
                "provider": "openai",
                "model": cfg.openai_review.model,
                "tier": "small",
                "store": False,
                "tools": False,
            },
        )
        return {"status": "approval_required", "approval_request_id": approval.id}

    @app.post("/build/sessions/{session_id}/review/approve")
    def build_review_approve(
        session_id: int, payload: BuildLeaseApproveRequest
    ) -> dict[str, Any]:
        require_build_session(session_id)
        if not hmac.compare_digest(
            payload.typed_confirmation.strip(),
            authority.summary()["typed_confirmation_phrase"],
        ):
            raise HTTPException(
                status_code=400,
                detail="OpenAI review lease requires the typed confirmation phrase.",
            )
        approval = db.get_pending_approval_for_source(
            source_type="openai_review_lease", source_id=session_id
        )
        if approval is None:
            raise HTTPException(status_code=400, detail="No pending OpenAI review lease approval")
        if payload.tier not in {None, BuildTier.SMALL.value}:
            raise HTTPException(
                status_code=400,
                detail="OpenAI review approval is limited to the displayed Small lease.",
            )
        tier = BuildTier.SMALL
        lease = build_store.create_lease(
            session_id,
            tier,
            cfg.build.limits(tier),
            provider="openai",
            model=cfg.openai_review.model,
            approval_request_id=approval.id,
        )
        db.resolve_approval_request(
            approval.id,
            status="approved",
            resolved_by="founder",
            resolution_note=payload.audit_note.strip() or "OpenAI review lease approved",
        )
        return {"status": "active", "lease": asdict(lease)}

    @app.post("/build/sessions/{session_id}/review/run")
    def build_review_run(
        session_id: int, payload: OpenAIReviewRunRequest
    ) -> dict[str, Any]:
        require_build_session(session_id)

        def authorize(request: Any) -> bool:
            lease = build_store.get_active_lease(session_id, provider="openai")
            if lease is None or request.session_id != session_id:
                return False
            decision = authorize_build_egress(
                db,
                EgressPolicy.from_config(cfg),
                EgressRequest(
                    request_id=request.request_id,
                    data_class=DataClass.SOURCE_CODE,
                    vendor="openai",
                    purpose=request.purpose,
                    byte_estimate=request.byte_estimate,
                ),
                lease=lease,
            )
            return decision.allowed

        try:
            result = openai_review_client(authorize).review(
                session_id=session_id,
                prompt=payload.prompt,
                context=build_service.review_context(session_id),
                request_id=payload.request_id,
            )
        except OpenAIReviewUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BuildBudgetExceeded as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return asdict(result)

    @app.get("/build/calibration")
    def build_calibrations(
        session_id: int | None = None,
        provider: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
        try:
            records = calibration.list(
                session_id=session_id, provider=provider, limit=limit
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"calibrations": [asdict(item) for item in records]}

    @app.post("/build/sessions/{session_id}/calibration")
    def build_calibration_record(
        session_id: int, payload: BuildCalibrationRequest
    ) -> dict[str, Any]:
        require_build_session(session_id)
        try:
            return {"calibration": asdict(calibration.record(
                session_id, provider=payload.provider, outcome=payload.outcome
            ))}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/build/managed-agents/readiness")
    def build_managed_agents_readiness() -> dict[str, Any]:
        return managed_agents.status(
            orchestration_ready=True,
            verification_ready=True,
            cancellation_ready=True,
            evidence_required=True,
        )

    # ---- Screen awareness ----
    @app.get("/screen/status")
    def screen_status() -> dict[str, Any]:
        return screen.status()

    @app.post("/screen/capture")
    def screen_capture(payload: ScreenCaptureRequest | None = None) -> dict[str, Any]:
        request = payload or ScreenCaptureRequest()
        try:
            return screen.capture(snapshot=request.snapshot)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- Shell auto-update manifest (local channel) ----
    @app.get("/shell/latest.json")
    def shell_update_manifest() -> dict[str, Any]:
        """Update manifest the desktop shell's updater polls over loopback.

        Local-first update channel: inert by default (version 0.0.0 is never newer
        than an installed build, so the updater reports "up to date"). To publish a
        local update, drop a Tauri-format manifest at ``data_dir/shell-update.json``
        (version, notes, pub_date, platforms{signature,url}) and it is served here.
        Pointing the shell's ``updater.endpoints`` at GitHub Releases instead is the
        one config change for remote auto-update.
        """
        override = cfg.paths.data_dir / "shell-update.json"
        if override.exists():
            try:
                return json.loads(override.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        return {
            "version": "0.0.0",
            "notes": "No shell update published on the local channel.",
            "pub_date": "1970-01-01T00:00:00Z",
            "platforms": {},
        }

    @app.get("/voice/status")
    def voice_status() -> dict[str, Any]:
        return voice.status()

    @app.post("/voice/transcribe")
    def voice_transcribe(payload: VoiceTranscribeRequest) -> dict[str, Any]:
        try:
            return voice.transcribe(audio_base64=payload.audio_base64, audio_mime=payload.audio_mime)
        except VoiceNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/voice/speak")
    def voice_speak(payload: VoiceSpeakRequest) -> dict[str, Any]:
        try:
            return voice.speak(text=payload.text)
        except VoiceNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/voice/converse")
    def voice_converse(payload: VoiceConverseRequest) -> dict[str, Any]:
        try:
            return voice.converse(**payload.model_dump())
        except VoiceNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except OllamaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/voice/converse/stream")
    def voice_converse_stream(payload: VoiceConverseRequest) -> StreamingResponse:
        """Streaming voice loop: NDJSON events (transcript -> token* -> response
        -> audio* -> done). Errors after the stream opens arrive as an
        {"type": "error"} event, not an HTTP status — a stream cannot change
        its status code mid-flight."""

        def event_lines() -> Any:
            try:
                for event in voice.converse_stream(**payload.model_dump()):
                    yield json.dumps(event) + "\n"
            except (VoiceNotConfigured, OllamaError, ValueError) as exc:
                yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"

        return StreamingResponse(event_lines(), media_type="application/x-ndjson")

    @app.post("/action-plans")
    def create_action_plan(payload: ActionPlanCreate) -> dict[str, Any]:
        try:
            return {"item": actions.create_plan(payload.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/action-plans/from-recommendation/{recommendation_id}")
    def create_action_plan_from_recommendation(
        recommendation_id: int,
        payload: ActionPlanFromRecommendation | None = None,
    ) -> dict[str, Any]:
        request = payload or ActionPlanFromRecommendation()
        try:
            return {
                "item": actions.create_plan_from_recommendation(
                    recommendation_id,
                    steps=[step.model_dump() for step in request.steps] or None,
                )
            }
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/action-plans")
    def list_action_plans(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": actions.list_plans(status=status, limit=limit)}

    @app.get("/action-plans/{plan_id}")
    def get_action_plan(plan_id: int) -> dict[str, Any]:
        try:
            return {"item": actions.get_plan(plan_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/action-plans/{plan_id}/advance")
    def advance_action_plan(plan_id: int) -> dict[str, Any]:
        try:
            return actions.advance(plan_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/action-plans/{plan_id}/steps/{step_id}/approve")
    def approve_action_step(plan_id: int, step_id: int, payload: ActionStepApprove | None = None) -> dict[str, Any]:
        request = payload or ActionStepApprove()
        try:
            return {"item": actions.approve_step(plan_id, step_id, approved_by=request.approved_by)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/action-plans/{plan_id}/steps/{step_id}/complete")
    def complete_action_step(plan_id: int, step_id: int, payload: ActionStepComplete | None = None) -> dict[str, Any]:
        request = payload or ActionStepComplete()
        try:
            return {"item": actions.complete_step(plan_id, step_id, **request.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/action-plans/{plan_id}/steps/{step_id}/fail")
    def fail_action_step(plan_id: int, step_id: int, payload: ActionStepFail) -> dict[str, Any]:
        try:
            return {"item": actions.fail_step(plan_id, step_id, **payload.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/action-plans/{plan_id}/steps/{step_id}/skip")
    def skip_action_step(plan_id: int, step_id: int, payload: ActionStepSkip | None = None) -> dict[str, Any]:
        request = payload or ActionStepSkip()
        try:
            return {"item": actions.skip_step(plan_id, step_id, note=request.note)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/action-plans/{plan_id}/steps/{step_id}/evidence")
    def attach_action_step_evidence(plan_id: int, step_id: int, payload: ActionStepEvidenceAttach) -> dict[str, Any]:
        try:
            return {"item": actions.attach_evidence(plan_id, step_id, evidence_id=payload.evidence_id)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/commitments")
    def create_commitment(payload: CommitmentCreate) -> dict[str, Any]:
        try:
            return {"item": commitments.create(payload.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/commitments")
    def list_commitments(
        status: str | None = None,
        who: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return {"items": commitments.list(status=status, who=who, kind=kind, limit=limit)}

    @app.post("/commitments/check")
    def check_commitments() -> dict[str, Any]:
        return commitments.check()

    @app.get("/commitments/{commitment_id}")
    def get_commitment(commitment_id: int) -> dict[str, Any]:
        try:
            return {"item": commitments.get(commitment_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/commitments/{commitment_id}/done")
    def complete_commitment(commitment_id: int, payload: CommitmentClose | None = None) -> dict[str, Any]:
        request = payload or CommitmentClose()
        try:
            return {"item": commitments.close(commitment_id, status="done", note=request.note, evidence_id=request.evidence_id)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/commitments/{commitment_id}/miss")
    def miss_commitment(commitment_id: int, payload: CommitmentClose | None = None) -> dict[str, Any]:
        request = payload or CommitmentClose()
        try:
            return {"item": commitments.close(commitment_id, status="missed", note=request.note, evidence_id=request.evidence_id)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/commitments/{commitment_id}/drop")
    def drop_commitment(commitment_id: int, payload: CommitmentClose | None = None) -> dict[str, Any]:
        request = payload or CommitmentClose()
        try:
            return {"item": commitments.close(commitment_id, status="dropped", note=request.note, evidence_id=request.evidence_id)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/commitments/{commitment_id}/renegotiate")
    def renegotiate_commitment(commitment_id: int, payload: CommitmentRenegotiate) -> dict[str, Any]:
        try:
            return {"item": commitments.renegotiate(commitment_id, due_at=payload.due_at, note=payload.note)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/notify")
    def send_notification(payload: NotifyRequest) -> dict[str, Any]:
        try:
            return {"item": bus.notify(**payload.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/notifications")
    def list_notifications(
        status: str | None = None,
        topic: str | None = None,
        unread_only: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {"items": bus.list(status=status, topic=topic, unread_only=unread_only, limit=limit)}

    @app.post("/notifications/{notification_id}/read")
    def read_notification(notification_id: int) -> dict[str, Any]:
        try:
            return {"item": bus.mark_read(notification_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/notify/channels")
    def list_notification_channels() -> dict[str, Any]:
        return {"items": bus.list_channels()}

    @app.post("/notify/channels/{channel}")
    def update_notification_channel(channel: str, payload: NotificationChannelUpdate) -> dict[str, Any]:
        try:
            return {"item": bus.update_channel(channel, payload.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/runtime/operating-loop")
    def runtime_operating_loop(payload: RuntimeLoopRequest | None = None) -> dict[str, Any]:
        request = payload or RuntimeLoopRequest()
        return runtime.operating_loop(**request.model_dump())

    @app.post("/runtime/evidence-loop")
    def runtime_evidence_loop(payload: EvidenceLoopRequest | None = None) -> dict[str, Any]:
        request = payload or EvidenceLoopRequest()
        return teaching.evidence_loop(**request.model_dump())

    @app.post("/runtime/experiment-loop")
    def runtime_experiment_loop(payload: ExperimentLoopRequest | None = None) -> dict[str, Any]:
        request = payload or ExperimentLoopRequest()
        return experiments.run_loop(**request.model_dump())

    @app.post("/runtime/cadence")
    def runtime_cadence(payload: CadenceRunRequest | None = None) -> dict[str, Any]:
        request = payload or CadenceRunRequest()
        operating = runtime.operating_loop(
            run_autonomous=request.run_autonomous,
            max_run=request.max_run,
            review_type=request.review_type,
            include_integrity=True,
            include_cadence=True,
        )
        evidence = teaching.evidence_loop(
            import_candidates=request.import_candidates,
            max_import=request.max_import,
            link_goals=request.link_goals,
            clear_resolved_warnings=request.clear_resolved_warnings,
            require_approval=request.require_approval,
        )
        experiment = experiments.run_loop(
            review_type=request.experiment_review_type,
            period=request.experiment_period,
            max_reviews=request.max_experiment_reviews,
        )
        commitment_check = commitments.check()
        surface = surfacing.brief()
        next_action = (
            surface["one_thing"]
            if surface["count"]
            else experiment.get("next_action") or operating.get("next_action")
        )
        audit_id = db.audit(
            actor="runtime",
            action="runtime.cadence",
            target=request.review_type,
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={
                "operating_event_id": operating.get("event_id"),
                "evidence_event_id": evidence.get("event_id"),
                "experiment_event_id": experiment.get("event_id"),
                "surfacing_event_id": surface.get("event_id"),
                "surfacing_item_count": surface.get("count"),
                "commitments_overdue": len(commitment_check.get("overdue", [])),
                "next_action": next_action,
            },
        )
        return {
            "generated_at": operating.get("generated_at"),
            "operating": operating,
            "evidence": evidence,
            "experiment": experiment,
            "commitments": commitment_check,
            "surfacing": surface,
            "audit_id": audit_id,
            "next_action": next_action,
        }

    @app.get("/runtime/events")
    def runtime_events(limit: int = 25) -> dict[str, Any]:
        return {"events": runtime.recent_events(limit=limit)}

    @app.post("/teach/deepthought/scan")
    def teach_deepthought_scan(payload: DeepThoughtScanRequest | None = None) -> dict[str, Any]:
        request = payload or DeepThoughtScanRequest()
        return teaching.scan(**request.model_dump())

    @app.get("/teach/deepthought/candidates")
    def teach_deepthought_candidates(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"candidates": teaching.list_candidates(status=status, limit=limit)}

    @app.post("/teach/deepthought/import")
    def teach_deepthought_import(payload: DeepThoughtImportRequest) -> dict[str, Any]:
        return teaching.import_candidates(**payload.model_dump())

    @app.post("/teach/deepthought/link")
    def teach_deepthought_link(payload: DeepThoughtLinkRequest) -> dict[str, Any]:
        return teaching.link_evidence(**payload.model_dump())

    @app.post("/teach/deepthought/auto-link")
    def teach_deepthought_auto_link(limit: int = 50) -> dict[str, Any]:
        return teaching.auto_link_imported(limit=limit)

    @app.get("/evidence/gaps")
    def evidence_gaps() -> dict[str, Any]:
        return teaching.evidence_gaps()

    # External-agent (MCP surface) memory writes held for founder approval.
    @app.get("/mcp/writes")
    def mcp_pending_writes() -> dict[str, Any]:
        from .agent_surface import list_pending_writes

        return {"pending": list_pending_writes(db)}

    @app.post("/mcp/writes/{request_id}/approve")
    def mcp_approve_write(request_id: int) -> dict[str, Any]:
        from .agent_surface import approve_pending_write

        try:
            return approve_pending_write(db, ingestion, request_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/mcp/writes/{request_id}/deny")
    def mcp_deny_write(request_id: int) -> dict[str, Any]:
        from .agent_surface import deny_pending_write

        try:
            return deny_pending_write(db, request_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # External-agent memory quarantined from grounding until the founder reviews it.
    @app.get("/memory/quarantine")
    def memory_quarantine_list(limit: int = 200) -> dict[str, Any]:
        return {"quarantined": db.list_memories_by_grounding_status("quarantined", limit=limit)}

    @app.post("/memory/quarantine/{memory_id}/release")
    def memory_quarantine_release(memory_id: int) -> dict[str, Any]:
        released = db.set_memory_grounding_status(memory_id, "active")
        if released is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")
        db.audit(
            actor="founder",
            action="memory.grounding.release",
            target=f"memory:{memory_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"memory_id": memory_id, "source": released.get("source")},
        )
        return {"released": released}

    @app.post("/memory/quarantine/{memory_id}/hold")
    def memory_quarantine_hold(memory_id: int) -> dict[str, Any]:
        held = db.set_memory_grounding_status(memory_id, "quarantined")
        if held is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")
        db.audit(
            actor="founder",
            action="memory.grounding.hold",
            target=f"memory:{memory_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"memory_id": memory_id, "source": held.get("source")},
        )
        return {"held": held}

    # What an external MCP client's memory.search may read. Private by default.
    @app.get("/memory/shareable")
    def memory_shareable_list(limit: int = 200) -> dict[str, Any]:
        return {"shareable": db.list_shareable_memories(limit=limit)}

    @app.post("/memory/{memory_id}/share")
    def memory_share(memory_id: int) -> dict[str, Any]:
        shared = db.set_memory_shareable(memory_id, True)
        if shared is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")
        db.audit(
            actor="founder",
            action="memory.share",
            target=f"memory:{memory_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"memory_id": memory_id, "source": shared.get("source")},
        )
        return {"shared": shared}

    @app.post("/memory/{memory_id}/unshare")
    def memory_unshare(memory_id: int) -> dict[str, Any]:
        unshared = db.set_memory_shareable(memory_id, False)
        if unshared is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")
        db.audit(
            actor="founder",
            action="memory.unshare",
            target=f"memory:{memory_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"memory_id": memory_id, "source": unshared.get("source")},
        )
        return {"unshared": unshared}

    # -- per-request egress grants (the egress gate's PER_REQUEST half) --------
    @app.get("/egress/grants")
    def egress_grants() -> dict[str, Any]:
        from .egress import list_pending_grants

        return {"pending": list_pending_grants(db)}

    @app.get("/egress/ledger")
    def egress_ledger_view(limit: int = 500) -> dict[str, Any]:
        """Founder-facing rollup: what left the machine, to whom, under which
        grant — and what the gate refused. Aggregates existing audit rows only;
        payloads are never stored, so none can appear here."""
        from .egress import egress_ledger

        return egress_ledger(db, limit=max(50, min(limit, 2000)))

    @app.post("/egress/grants/{request_id}/approve")
    def egress_approve_grant(request_id: int, payload: ApprovalResolveRequest | None = None) -> dict[str, Any]:
        from .egress import approve_egress_grant

        request = payload or ApprovalResolveRequest()
        try:
            return approve_egress_grant(
                db,
                request_id,
                resolved_by=request.resolved_by,
                typed_phrase=request.typed_confirmation,
                typed_confirmation_phrase=authority.summary()["typed_confirmation_phrase"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/egress/grants/{request_id}/deny")
    def egress_deny_grant(request_id: int, payload: ApprovalResolveRequest | None = None) -> dict[str, Any]:
        from .egress import deny_egress_grant

        request = payload or ApprovalResolveRequest()
        try:
            return deny_egress_grant(db, request_id, resolved_by=request.resolved_by, note=request.note)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # -- founder_brief → Anthropic strategic review (first cloud consumer) -----
    @app.post("/strategy/review")
    def strategy_review_request(payload: StrategyReviewRequest | None = None) -> dict[str, Any]:
        body = payload or StrategyReviewRequest()
        return strategy_review.request_review(focus=body.focus, question=body.question)

    @app.get("/strategy/reviews")
    def strategy_review_pending() -> dict[str, Any]:
        return {"pending": strategy_review.list_pending()}

    @app.post("/strategy/reviews/{request_id}/approve")
    def strategy_review_approve(request_id: int, payload: ApprovalResolveRequest | None = None) -> dict[str, Any]:
        request = payload or ApprovalResolveRequest()
        try:
            return strategy_review.approve(request_id, resolved_by=request.resolved_by, typed_phrase=request.typed_confirmation)
        except AnthropicNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AnthropicError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/strategy/reviews/{request_id}/deny")
    def strategy_review_deny(request_id: int, payload: ApprovalResolveRequest | None = None) -> dict[str, Any]:
        request = payload or ApprovalResolveRequest()
        try:
            return strategy_review.deny(request_id, resolved_by=request.resolved_by, note=request.note)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # -- cross-channel founder authentication (prereq for any channel ingress) -
    @app.post("/channels/enroll")
    def channel_enroll(payload: ChannelEnrollRequest) -> dict[str, Any]:
        # Returns a ONE-TIME code the founder echoes through the target channel.
        return channel_auth.begin_enrollment(payload.channel, label=payload.label)

    @app.post("/channels/confirm")
    def channel_confirm(payload: ChannelConfirmRequest) -> dict[str, Any]:
        # The channel adapter calls this when an inbound message carries the code.
        try:
            identity = channel_auth.confirm_enrollment(payload.channel, payload.external_id, payload.code)
        except ChannelAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"authenticated": identity.authenticated, "binding_id": identity.binding_id, "max_tier": identity.max_tier}

    @app.get("/channels/bindings")
    def channel_bindings(include_revoked: bool = False) -> dict[str, Any]:
        return {"bindings": channel_auth.list_bindings(include_revoked=include_revoked)}

    @app.post("/channels/bindings/{binding_id}/revoke")
    def channel_revoke(binding_id: int) -> dict[str, Any]:
        try:
            return channel_auth.revoke(binding_id)
        except ChannelAuthError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/channels/bindings/{binding_id}/tier")
    def channel_set_tier(binding_id: int, payload: ChannelTierRequest) -> dict[str, Any]:
        try:
            return channel_auth.set_max_tier(binding_id, payload.max_tier)
        except ChannelAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def handle_channel_message(
        *, channel: str, external_id: str, text: str, ts: str = "", signature: str = ""
    ) -> dict[str, Any]:
        """The governed channel-message flow, shared by the HTTP endpoint and the
        in-process OpenClaw bridge. A '/bind <code>' message completes an
        enrollment; otherwise the identity is authenticated (with the binding's
        HMAC policy) and, ONLY if bound, routed into the runtime capped at its
        authority ceiling on a durable per-binding thread. Unbound identities are
        refused — they never reach the authority-bearing runtime."""
        code = parse_bind_command(text)
        if code is not None:
            try:
                identity = channel_auth.confirm_enrollment(channel, external_id, code)
            except ChannelAuthError as exc:
                return {"status": "bind_failed", "authenticated": False, "reply": str(exc)}
            return {
                "status": "bound", "authenticated": True, "binding_id": identity.binding_id,
                "max_tier": identity.max_tier, "reply": "This channel is now bound to the founder.",
            }

        identity = channel_auth.authenticate_message(
            channel, external_id, text=text, ts=ts, signature=signature
        )
        if not identity.authenticated:
            return {
                "status": "unauthenticated", "authenticated": False,
                "reply": ("This channel is not authorized. The founder can enroll it from Zade, "
                          "then send '/bind <code>' here to confirm."),
            }

        # Per-binding conversation continuity: messages from a bound identity share
        # one durable thread instead of arriving as standalone amnesiac turns. If a
        # distillation sweep has since finalized the stored thread, a fresh one is
        # bound — continuity resets rather than piling onto an ended conversation.
        assert identity.binding_id is not None
        conversation_id = channel_auth.conversation_id_for(identity.binding_id)
        if conversation_id is not None:
            existing = db.get_conversation(conversation_id)
            if not existing or existing.get("status") != "active":
                conversation_id = None
        if conversation_id is None:
            created = conversations.create(
                title=f"channel: {channel} · {identity.label or external_id}",
                metadata={"channel": channel, "external_id": external_id, "binding_id": identity.binding_id},
            )
            conversation_id = int(created["id"])
            channel_auth.set_conversation_id(identity.binding_id, conversation_id)

        # Bound founder identity — route into the runtime, capped at its ceiling.
        result = runtime.respond(
            message=text,
            authority_ceiling=identity.max_tier,
            conversation_id=conversation_id,
            use_tools=False,  # like voice: answer from context, no per-message tool loop
        )
        return {
            "status": "ok", "authenticated": True, "max_tier": identity.max_tier,
            "channel_capped": result.get("channel_capped"),
            "conversation_id": conversation_id,
            "reply": result["response"], "event_id": result["event_id"],
        }

    @app.post("/channels/message")
    def channel_message(payload: ChannelMessageRequest) -> dict[str, Any]:
        """Channel-adapter ingress over HTTP. Mutation-token gated, so only the
        trusted local adapter can inject messages. Delegates to the shared
        governed flow."""
        return handle_channel_message(
            channel=payload.channel,
            external_id=payload.external_id,
            text=payload.text,
            ts=payload.ts,
            signature=payload.signature,
        )

    # OpenClaw channel-gateway bridge (external transport). Off unless
    # [openclaw] enabled + a token is set. It routes inbound channel messages
    # through the SAME governed flow above (in-process, so channel auth + capped
    # authority + HMAC all apply), and sends Zade's reply back via the gateway.
    def _route_openclaw(inbound: InboundMessage) -> dict[str, Any]:
        return handle_channel_message(
            channel=inbound.channel, external_id=inbound.external_id, text=inbound.text
        )

    openclaw_bridge = OpenClawBridge(cfg.openclaw, route_message=_route_openclaw)
    app.state.openclaw_bridge = openclaw_bridge
    # Started below only under serving-boot (run_boot_maintenance), so merely
    # constructing the app for introspection never opens a gateway socket.
    if run_boot_maintenance and cfg.openclaw.enabled:
        try:
            openclaw_bridge.start()
            atexit.register(openclaw_bridge.stop)
        except OpenClawBridgeError:
            # Misconfiguration (no token, remote gateway without opt-in) must not
            # block kernel startup — the bridge simply stays down and /status
            # shows it. Everything else in the kernel runs local-only regardless.
            pass

    @app.get("/channels/openclaw/status")
    def openclaw_status() -> dict[str, Any]:
        return {
            "enabled": cfg.openclaw.enabled,
            "ws_url": cfg.openclaw.ws_url,
            "token_present": bool(os.getenv(cfg.openclaw.token_env, "")),
            "connected": bool(getattr(openclaw_bridge, "_ws", None) is not None),
        }

    # Direct Telegram Bot API adapter — Zade's own channel transport (no OpenClaw).
    # Off unless [telegram] enabled + TELEGRAM_BOT_TOKEN is set. Routes inbound
    # messages through the SAME governed flow, gates replies through the egress
    # matrix (reply_text:telegram standing grant), and sends via sendMessage.
    def _route_telegram(inbound: InboundTelegram) -> dict[str, Any]:
        return handle_channel_message(
            channel="telegram", external_id=inbound.external_id, text=inbound.text
        )

    telegram_adapter = TelegramAdapter(cfg, route_message=_route_telegram, db=db)
    app.state.telegram_adapter = telegram_adapter
    if run_boot_maintenance and cfg.telegram.enabled:
        try:
            telegram_adapter.start()
            atexit.register(telegram_adapter.stop)
        except TelegramError:
            # No token / misconfig must not block startup; /status shows it down.
            pass

    # Kernel heartbeat: enabled-but-down channel alerts, cadence-staleness
    # alerts, and the scheduled morning brief (see heartbeat.py). The brief
    # sender reuses the adapter's minimal client; bound founder chats come from
    # channel_auth (1:1 chat_id == the bound external_id).
    def _telegram_founder_chats() -> list[int]:
        chats: list[int] = []
        for binding in channel_auth.list_bindings():
            if binding.get("channel") != "telegram":
                continue
            try:
                chats.append(int(binding["external_id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return chats

    _tg_token = token_from_env(cfg.telegram.token_env)
    _tg_client = TelegramClient(cfg.telegram, _tg_token) if _tg_token else None
    heartbeat = KernelHeartbeat(
        cfg,
        db=db,
        founder=founder,
        notify=bus,
        telegram_running=lambda: telegram_adapter.running,
        telegram_chat_ids=_telegram_founder_chats,
        send_telegram=_tg_client.send_message if _tg_client else None,
    )
    app.state.heartbeat = heartbeat
    if run_boot_maintenance:
        heartbeat.start()
        atexit.register(heartbeat.stop)

    @app.get("/channels/telegram/status")
    def telegram_status() -> dict[str, Any]:
        return {
            "enabled": cfg.telegram.enabled,
            "token_present": bool(token_from_env(cfg.telegram.token_env)),
            "running": telegram_adapter.running,
            "brief": {
                "enabled": cfg.telegram.brief_enabled,
                "time": cfg.telegram.brief_time,
                "last": heartbeat.last_brief,
            },
        }

    @app.post("/channels/telegram/brief")
    def telegram_brief_now() -> dict[str, Any]:
        """Founder-triggered brief push (bypasses the once-per-day gate, never
        the egress gate). Mutation-token protected like every other POST."""
        return {"result": heartbeat.send_now()}

    @app.post("/channels/bindings/{binding_id}/hmac")
    def channel_issue_hmac(binding_id: int) -> dict[str, Any]:
        """Issue (or rotate) a per-binding signing key — for adapters/bots that can
        sign each message. Returned ONCE; from then on every inbound message from
        the binding must carry ts + HMAC-SHA256(key, f"{ts}\\n{text}")."""
        try:
            return channel_auth.issue_hmac_key(binding_id)
        except ChannelAuthError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/channels/bindings/{binding_id}/hmac/clear")
    def channel_clear_hmac(binding_id: int) -> dict[str, Any]:
        try:
            return channel_auth.clear_hmac_key(binding_id)
        except ChannelAuthError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/experiments")
    def list_experiments(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": experiments.list_experiments(status=status, limit=limit)}

    @app.post("/experiments")
    def create_experiment(payload: ExperimentCreate) -> dict[str, Any]:
        return {"item": experiments.create_experiment(payload.model_dump())}

    @app.get("/experiments/dashboard")
    def experiments_dashboard() -> dict[str, Any]:
        return experiments.dashboard()

    @app.get("/experiments/reviews")
    def list_experiment_reviews(experiment_id: int | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": experiments.list_reviews(experiment_id=experiment_id, limit=limit)}

    @app.get("/experiments/{experiment_id}")
    def get_experiment(experiment_id: int) -> dict[str, Any]:
        try:
            return {"item": experiments.get_experiment(experiment_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/experiments/{experiment_id}/evidence")
    def add_experiment_evidence(experiment_id: int, payload: ExperimentEvidenceCreate) -> dict[str, Any]:
        try:
            return experiments.add_evidence(experiment_id, payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/experiments/{experiment_id}/review")
    def review_experiment(experiment_id: int, payload: ExperimentReviewCreate) -> dict[str, Any]:
        try:
            return experiments.review_experiment(experiment_id, payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/experiments/{experiment_id}/update")
    def update_experiment(experiment_id: int, payload: ExperimentUpdate) -> dict[str, Any]:
        try:
            return experiments.update_experiment(experiment_id, payload.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/experiments/{experiment_id}/pushback")
    def pushback_experiment(experiment_id: int, payload: ExperimentPushbackCreate) -> dict[str, Any]:
        try:
            return experiments.pushback(experiment_id, payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/work/queue")
    def list_work_queue(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": work_queue.list_items(status=status, limit=limit), "counts": db.work_queue_counts()}

    @app.post("/work/items")
    def create_work_item(payload: WorkItemCreate) -> dict[str, Any]:
        result = work_queue.enqueue(
            kind=payload.kind,
            title=payload.title,
            detail=payload.detail,
            action=payload.action,
            target=payload.target,
            permission_tier=payload.permission_tier,
            priority=payload.priority,
            source=payload.source,
            due_at=payload.due_at,
            metadata=payload.metadata,
            unique_key=payload.unique_key,
        )
        return result.as_dict()

    @app.post("/work/scan")
    def scan_work(payload: WorkScanRequest | None = None) -> dict[str, Any]:
        request = payload or WorkScanRequest()
        return work_queue.scan(run_autonomous=request.run_autonomous, max_run=request.max_run)

    @app.post("/work/run-next")
    def run_next_work() -> dict[str, Any]:
        return work_queue.run_next().as_dict()

    @app.post("/work/run-due")
    def run_due_work(payload: WorkRunRequest | None = None) -> dict[str, Any]:
        request = payload or WorkRunRequest()
        return {"results": [result.as_dict() for result in work_queue.run_due(max_items=request.max_items)]}

    @app.get("/approval-requests")
    def list_approval_requests(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": approvals.list_requests(status=status, limit=limit)}

    @app.get("/approval-console")
    def approval_console(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return approvals.list_console(status=status, limit=limit)

    @app.get("/approval-console/{request_id}")
    def approval_console_item(request_id: int) -> dict[str, Any]:
        try:
            return {"item": approvals.get_console_item(request_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/approval-training-events")
    def approval_training_events(
        approval_request_id: int | None = None,
        outcome: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {"items": approvals.list_training_events(
            approval_request_id=approval_request_id,
            outcome=outcome,
            limit=limit,
        )}

    @app.get("/action-handlers")
    def list_action_handlers() -> dict[str, Any]:
        return {"items": approvals.list_handlers()}

    @app.post("/action-handlers/{action}/enable")
    def enable_action_handler(action: str) -> dict[str, Any]:
        try:
            return {"item": approvals.set_handler_access(action, enabled=True)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/action-handlers/{action}/disable")
    def disable_action_handler(action: str) -> dict[str, Any]:
        try:
            return {"item": approvals.set_handler_access(action, enabled=False)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/trading-bot/status")
    def trading_bot_status() -> dict[str, Any]:
        return trading_bot.status()

    @app.get("/trading-bot/safe-ops-checks")
    def trading_bot_safe_ops_checks() -> dict[str, Any]:
        return {"items": trading_bot.safe_ops_checks()}

    @app.get("/trading-bot/deep-thought-replacement")
    def trading_bot_deep_thought_replacement() -> dict[str, Any]:
        return trading_bot.deep_thought_replacement_map()

    @app.get("/trading-bot/intelligence/access")
    def trading_bot_intelligence_access() -> dict[str, Any]:
        return trading_bot.intelligence_access()

    @app.get("/trading-bot/sqlite/schema")
    def trading_bot_sqlite_schema(
        database: str = "trades.db",
        table: str | None = None,
        include_counts: bool = False,
    ) -> dict[str, Any]:
        try:
            return trading_bot.sqlite_schema(database=database, table=table, include_counts=include_counts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/trading-bot/sqlite/query")
    def trading_bot_sqlite_query(payload: TradingBotSQLiteQueryRequest) -> dict[str, Any]:
        try:
            return trading_bot.run_sqlite_query(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/trading-bot/training/run")
    def trading_bot_training_run(payload: TradingBotTrainingRunRequest) -> dict[str, Any]:
        try:
            return trading_bot.run_training(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/trading-bot/events/recent")
    def trading_bot_recent_events(
        limit: int = 50,
        event_type: str | None = None,
        symbol: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        try:
            return trading_bot.recent_events(limit=limit, event_type=event_type, symbol=symbol, since=since)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/trading-bot/signals/recent")
    def trading_bot_recent_signals(limit: int = 50, symbol: str | None = None) -> dict[str, Any]:
        try:
            return trading_bot.recent_signals(limit=limit, symbol=symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/trading-bot/market-context")
    def trading_bot_market_context(
        target_date: str | None = None,
        symbol: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        try:
            return trading_bot.market_context(target_date=target_date, symbol=symbol, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/trading-bot/evidence/snapshot")
    def trading_bot_evidence_snapshot(payload: TradingBotEvidenceSnapshotRequest) -> dict[str, Any]:
        try:
            return trading_bot.evidence_snapshot(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/trading-bot/ops-check")
    def trading_bot_ops_check(payload: TradingBotOpsCheckRequest) -> dict[str, Any]:
        try:
            return trading_bot.run_ops_check(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/trading-bot/recommendations")
    def queue_trading_bot_recommendation(payload: TradingBotRecommendationCreate) -> dict[str, Any]:
        try:
            return trading_bot.queue_advisory_recommendation(
                work_queue=work_queue,
                payload=payload.model_dump(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/trading-bot/advisory/generate")
    def generate_trading_bot_advisory(payload: TradingBotAdvisoryGenerateRequest) -> dict[str, Any]:
        try:
            return trading_bot.generate_advisory_recommendations(
                work_queue=work_queue,
                **payload.model_dump(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/trading-bot/advisory/score")
    def score_trading_bot_advisory(payload: TradingBotAdvisoryScoreRequest) -> dict[str, Any]:
        try:
            return trading_bot.score_advisory_outcomes(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/trading-bot/judgments/score")
    def score_trading_bot_judgments(payload: TradingBotJudgmentScoreRequest) -> dict[str, Any]:
        try:
            return trading_bot.score_judgments_against_outcomes(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/trading-bot/dt-trigger/proposals")
    def queue_trading_bot_dt_trigger_proposal(payload: TradingBotTriggerProposalRequest) -> dict[str, Any]:
        try:
            return trading_bot.queue_dt_trigger_proposal(work_queue=work_queue, payload=payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/trading-bot/daily-brief")
    def trading_bot_daily_brief(payload: TradingBotDailyBriefRequest) -> dict[str, Any]:
        try:
            return trading_bot.daily_trading_brief(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/trading-bot/judgments")
    def trading_bot_judgments(
        market_date: str | None = None,
        symbol: str | None = None,
        outcome_status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            return {
                "items": trading_bot.list_trading_judgments(
                    market_date=market_date,
                    symbol=symbol,
                    outcome_status=outcome_status,
                    limit=limit,
                )
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/approval-requests/{request_id}")
    def get_approval_request(request_id: int) -> dict[str, Any]:
        try:
            return {"item": approvals.get_request(request_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/approval-requests/{request_id}/approve")
    def approve_request(request_id: int, payload: ApprovalResolveRequest | None = None) -> dict[str, Any]:
        request = payload or ApprovalResolveRequest()
        approval = db.get_approval_request(request_id)
        if approval and approval.source_type in {
            BUILD_LEASE_SOURCE,
            BUILD_UPGRADE_SOURCE,
        }:
            if approval.source_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="Build lease approval has no build session.",
                )
            build = build_session_approve(
                approval.source_id,
                BuildLeaseApproveRequest(
                    typed_confirmation=request.typed_confirmation,
                    audit_note=request.note,
                ),
            )
            return {
                "specialized_approval": approval.source_type,
                "request": approvals.get_request(request_id),
                "build": build,
            }
        try:
            return approvals.approve_request(
                request_id,
                resolved_by=request.resolved_by,
                note=request.note,
                dispatch=request.dispatch,
                typed_confirmation=request.typed_confirmation,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/approval-requests/{request_id}/deny")
    def deny_request(request_id: int, payload: ApprovalResolveRequest | None = None) -> dict[str, Any]:
        request = payload or ApprovalResolveRequest()
        try:
            return approvals.deny_request(request_id, resolved_by=request.resolved_by, note=request.note)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/approval-requests/{request_id}/defer")
    def defer_request(request_id: int, payload: ApprovalDeferRequest | None = None) -> dict[str, Any]:
        request = payload or ApprovalDeferRequest()
        try:
            return approvals.defer_request(
                request_id,
                resolved_by=request.resolved_by,
                note=request.note,
                defer_until=request.defer_until,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/approval-requests/{request_id}/edit")
    def edit_request(request_id: int, payload: ApprovalEditRequest) -> dict[str, Any]:
        try:
            return approvals.edit_request(
                request_id,
                edited_by=payload.edited_by,
                note=payload.note,
                title=payload.title,
                detail=payload.detail,
                action=payload.action,
                target=payload.target,
                permission_tier=payload.permission_tier,
                priority=payload.priority,
                evidence=payload.evidence,
                risks=payload.risks,
                metadata=payload.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/work/items/{item_id}/approve")
    def approve_work_item(item_id: int, payload: ApprovalResolveRequest | None = None) -> dict[str, Any]:
        request = payload or ApprovalResolveRequest()
        try:
            return approvals.approve_work_item(
                item_id,
                resolved_by=request.resolved_by,
                note=request.note,
                dispatch=request.dispatch,
                typed_confirmation=request.typed_confirmation,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/work/items/{item_id}/dispatch")
    def dispatch_work_item(item_id: int, payload: ApprovalResolveRequest | None = None) -> dict[str, Any]:
        request = payload or ApprovalResolveRequest()
        try:
            return approvals.dispatch_work_item(item_id, typed_confirmation=request.typed_confirmation)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/work/items/{item_id}/deny")
    def deny_work_item(item_id: int, payload: ApprovalResolveRequest | None = None) -> dict[str, Any]:
        request = payload or ApprovalResolveRequest()
        try:
            return approvals.deny_work_item(item_id, resolved_by=request.resolved_by, note=request.note)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/work/items/{item_id}/defer")
    def defer_work_item(item_id: int, payload: ApprovalDeferRequest | None = None) -> dict[str, Any]:
        request = payload or ApprovalDeferRequest()
        try:
            return approvals.defer_work_item(
                item_id,
                resolved_by=request.resolved_by,
                note=request.note,
                defer_until=request.defer_until,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/work/items/{item_id}/edit")
    def edit_work_item(item_id: int, payload: ApprovalEditRequest) -> dict[str, Any]:
        try:
            return approvals.edit_work_item(
                item_id,
                edited_by=payload.edited_by,
                note=payload.note,
                title=payload.title,
                detail=payload.detail,
                action=payload.action,
                target=payload.target,
                permission_tier=payload.permission_tier,
                priority=payload.priority,
                evidence=payload.evidence,
                risks=payload.risks,
                metadata=payload.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/founder/mental-models")
    def founder_mental_models() -> dict[str, Any]:
        return {"models": founder.mental_models()}

    @app.get("/founder/thesis")
    def founder_thesis() -> dict[str, Any]:
        return {"thesis": founder.get_thesis()}

    @app.post("/founder/thesis")
    def upsert_founder_thesis(payload: CompanyThesisUpsert) -> dict[str, Any]:
        return {"thesis": founder.upsert_thesis(payload.model_dump())}

    @app.get("/founder/dashboard")
    def founder_dashboard() -> dict[str, Any]:
        return founder.dashboard()

    @app.get("/founder/metrics")
    def founder_metrics() -> dict[str, Any]:
        return _founder_metrics(db)

    @app.get("/founder/brief")
    def founder_brief() -> dict[str, Any]:
        return founder.brief()

    @app.get("/founder/active-objective")
    def founder_active_objective() -> dict[str, Any]:
        return {"item": founder.get_active_objective()}

    @app.get("/founder/active-objectives")
    def list_founder_active_objectives(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_active_objectives(status=status, limit=limit)}

    @app.post("/founder/active-objectives")
    def create_founder_active_objective(payload: ActiveObjectiveCreate) -> dict[str, Any]:
        result = founder.create_active_objective(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.post("/founder/active-objectives/{objective_id}/activate")
    def activate_founder_active_objective(objective_id: int) -> dict[str, Any]:
        try:
            return {"item": founder.activate_objective(objective_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/founder/active-objectives/{objective_id}/status")
    def update_founder_active_objective_status(objective_id: int, payload: ActiveObjectiveStatusUpdate) -> dict[str, Any]:
        try:
            return {"item": founder.update_active_objective_status(objective_id, status=payload.status, note=payload.note)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/founder/decision-recommendations")
    def list_founder_decision_recommendations(
        status: str | None = None,
        objective_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {"items": founder.list_decision_recommendations(status=status, objective_id=objective_id, limit=limit)}

    @app.get("/founder/decision-recommendations/{recommendation_id}")
    def get_founder_decision_recommendation(recommendation_id: int) -> dict[str, Any]:
        try:
            return {"item": founder.get_decision_recommendation(recommendation_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/founder/decision-engine/recommend")
    def founder_decision_engine_recommend(payload: DecisionEngineRequest) -> dict[str, Any]:
        try:
            return founder.recommend_decision(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/founder/strategy")
    def list_founder_strategy(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_strategy_entries(status=status, limit=limit)}

    @app.post("/founder/strategy")
    def create_founder_strategy(payload: StrategyEntryCreate) -> dict[str, Any]:
        result = founder.create_strategy_entry(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/initiatives")
    def list_founder_initiatives(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_initiatives(status=status, limit=limit)}

    @app.post("/founder/initiatives")
    def create_founder_initiative(payload: InitiativeCreate) -> dict[str, Any]:
        result = founder.create_initiative(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/decisions")
    def list_founder_decisions(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_decision_memos(status=status, limit=limit)}

    @app.post("/founder/decisions")
    def create_founder_decision(payload: DecisionMemoCreate) -> dict[str, Any]:
        result = founder.create_decision_memo(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/predictions")
    def list_founder_predictions(result: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_predictions(result=result, limit=limit)}

    @app.post("/founder/predictions")
    def create_founder_prediction(payload: FounderPredictionCreate) -> dict[str, Any]:
        result = founder.create_prediction(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.post("/founder/predictions/score")
    def score_founder_prediction(payload: FounderPredictionScore) -> dict[str, Any]:
        try:
            return {"item": founder.score_prediction(payload.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/founder/contrarian-reviews")
    def list_founder_contrarian_reviews(limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_contrarian_reviews(limit=limit)}

    @app.post("/founder/contrarian-reviews")
    def create_founder_contrarian_review(payload: ContrarianReviewCreate) -> dict[str, Any]:
        result = founder.create_contrarian_review(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/reflections")
    def list_founder_reflections(limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_reflections(limit=limit)}

    @app.post("/founder/reflections")
    def create_founder_reflection(payload: ReflectionCreate) -> dict[str, Any]:
        result = founder.create_reflection(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/assumptions")
    def list_founder_assumptions(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_assumptions(status=status, limit=limit)}

    @app.post("/founder/assumptions")
    def create_founder_assumption(payload: AssumptionCreate) -> dict[str, Any]:
        result = founder.create_assumption(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/evidence")
    def list_founder_evidence(limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_evidence(limit=limit)}

    @app.post("/founder/evidence")
    def create_founder_evidence(payload: EvidenceCreate) -> dict[str, Any]:
        result = founder.create_evidence(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/links")
    def list_founder_links(limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_links(limit=limit)}

    @app.post("/founder/links")
    def create_founder_link(payload: ObjectLinkCreate) -> dict[str, Any]:
        result = founder.create_link(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/strategy-objects")
    def list_founder_strategy_objects(object_type: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_strategy_objects(object_type=object_type, limit=limit)}

    @app.post("/founder/strategy-objects")
    def create_founder_strategy_object(payload: StrategyObjectCreate) -> dict[str, Any]:
        result = founder.create_strategy_object(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/goals")
    def list_founder_goals(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_goals(status=status, limit=limit)}

    @app.post("/founder/goals")
    def create_founder_goal(payload: FounderGoalCreate) -> dict[str, Any]:
        result = founder.create_goal(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/tasks")
    def list_founder_tasks(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_tasks(status=status, limit=limit)}

    @app.post("/founder/tasks")
    def create_founder_task(payload: FounderTaskCreate) -> dict[str, Any]:
        result = founder.create_task(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/kill-criteria")
    def list_founder_kill_criteria(limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_kill_criteria(limit=limit)}

    @app.post("/founder/kill-criteria")
    def create_founder_kill_criteria(payload: KillCriteriaCreate) -> dict[str, Any]:
        result = founder.create_kill_criteria(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/overrides")
    def list_founder_overrides(limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_overrides(limit=limit)}

    @app.post("/founder/overrides")
    def create_founder_override(payload: FounderOverrideCreate) -> dict[str, Any]:
        result = founder.create_override(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/confidence-events")
    def list_founder_confidence_events(limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_confidence_events(limit=limit)}

    @app.get("/founder/thesis-conflicts")
    def list_founder_thesis_conflicts(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_thesis_conflicts(status=status, limit=limit)}

    @app.post("/founder/thesis-conflicts")
    def create_founder_thesis_conflict(payload: ThesisConflictCreate) -> dict[str, Any]:
        return {"item": founder.detect_thesis_conflict(payload.model_dump())}

    @app.get("/founder/missed-calls")
    def list_founder_missed_calls(limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_missed_call_reviews(limit=limit)}

    @app.post("/founder/missed-calls")
    def create_founder_missed_call(payload: MissedCallReviewCreate) -> dict[str, Any]:
        result = founder.create_missed_call_review(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.get("/founder/integrity-warnings")
    def list_founder_integrity_warnings(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_integrity_warnings(status=status, limit=limit)}

    @app.post("/founder/integrity-check")
    def run_founder_integrity_check() -> dict[str, Any]:
        return founder.run_integrity_check()

    @app.get("/founder/cadence-reviews")
    def list_founder_cadence_reviews(review_type: str | None = None, limit: int = 50) -> dict[str, Any]:
        return {"items": founder.list_cadence_reviews(review_type=review_type, limit=limit)}

    @app.post("/founder/cadence-reviews")
    def create_founder_cadence_review(payload: CadenceReviewCreate) -> dict[str, Any]:
        result = founder.create_cadence_review(payload.model_dump())
        return {"id": result.id, "item": result.record}

    @app.post("/founder/cadence-reviews/generate/{review_type}")
    def generate_founder_cadence_review(review_type: str, period: str | None = None) -> dict[str, Any]:
        return {"item": founder.generate_cadence_review(review_type=review_type, period=period)}

    @app.post("/memory")
    def create_memory(payload: MemoryCreate) -> dict[str, Any]:
        result = tools.call(
            "memory.write",
            {
                "kind": payload.kind,
                "title": payload.title,
                "content": payload.content,
                "source": payload.source,
                "metadata": payload.metadata,
            },
            actor="api",
        )
        if not result.ok:
            raise HTTPException(status_code=400, detail=_tool_error(result.data))
        return result.data

    @app.post("/memory/search")
    def search_memory(payload: MemorySearch) -> dict[str, Any]:
        result = tools.call("memory.search", payload.model_dump(), actor="api")
        if not result.ok:
            raise HTTPException(status_code=400, detail=_tool_error(result.data))
        return result.data

    @app.get("/memory/stats")
    def memory_stats() -> dict[str, Any]:
        """Counts backing the Memory surface: hot = memory rows on this machine,
        cold = ingested documents (and their chunks) in the archive."""
        return db.memory_stats()

    @app.delete("/memory/{memory_id}")
    def forget_memory(memory_id: int) -> dict[str, Any]:
        """Founder-commanded forget: removes the memory row and its FTS entry.
        Runs through the tool registry so it is audited at L1_MEMORY_WRITE."""
        result = tools.call("memory.forget", {"memory_id": memory_id}, actor="api")
        if not result.ok:
            status_code = 404 if result.data.get("error") == "memory_not_found" else 400
            raise HTTPException(status_code=status_code, detail=_tool_error(result.data))
        return result.data

    @app.post("/ingest/text")
    def ingest_text(payload: IngestTextRequest) -> dict[str, Any]:
        result = ingestion.ingest_text(
            title=payload.title,
            text=payload.text,
            source=payload.source,
            metadata=payload.metadata,
        )
        if result.status == "error":
            raise HTTPException(status_code=400, detail=result.error or "Ingestion failed.")
        return result.__dict__

    @app.post("/ingest/file")
    def ingest_file(payload: IngestFileRequest) -> dict[str, Any]:
        try:
            result = ingestion.ingest_file(path=payload.path, metadata=payload.metadata)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result.status == "error":
            raise HTTPException(status_code=400, detail=result.error or "Ingestion failed.")
        return result.__dict__

    @app.post("/ingest/folder")
    def ingest_folder(payload: IngestFolderRequest) -> dict[str, Any]:
        try:
            result = ingestion.ingest_folder(path=payload.path, recursive=payload.recursive, metadata=payload.metadata)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=str(result.get("errors") or "Ingestion failed."))
        return result

    @app.get("/ingest/jobs")
    def ingest_jobs(limit: int = 25) -> dict[str, Any]:
        return {"jobs": db.recent_ingestion_jobs(limit=limit)}

    @app.post("/memory/semantic-search")
    def semantic_search(payload: SemanticSearchRequest) -> dict[str, Any]:
        try:
            matches = ingestion.semantic_search(query=payload.query, limit=payload.limit, mode=payload.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OllamaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"matches": matches}

    @app.get("/audit/recent")
    def recent_audit(limit: int = 25) -> dict[str, Any]:
        result = tools.call("audit.recent", {"limit": limit}, actor="api")
        if not result.ok:
            raise HTTPException(status_code=400, detail=_tool_error(result.data))
        return result.data

    @app.get("/brief/daily")
    def daily_brief() -> dict[str, Any]:
        brief = build_daily_brief(db)
        db.audit(
            actor="api",
            action="brief.daily",
            target="local_memory",
            permission_tier="L0_READ",
            status="ok",
            details={"sections": list(brief["inputs"].keys())},
        )
        return brief

    @app.post("/chat", response_model=ChatResponse)
    def chat(payload: ChatRequest) -> ChatResponse:
        memory_hits = []
        semantic_hits = []
        if payload.use_memory:
            # include_quarantined=False so the hits shown to the founder match what
            # actually grounded the answer (runtime.respond filters the same way).
            memory_hits = [record.__dict__ for record in db.search_memories(payload.message, limit=5, include_quarantined=False)]
        if payload.use_memory and payload.use_semantic_memory and payload.semantic_limit > 0:
            try:
                semantic_hits = ingestion.semantic_search(query=payload.message, limit=payload.semantic_limit)
            except OllamaError:
                semantic_hits = []

        try:
            result = runtime.respond(
                message=payload.message,
                task_type=payload.task_type,
                model=payload.model,
                profile=payload.profile,
                use_memory=payload.use_memory,
                use_semantic_memory=payload.use_semantic_memory,
                semantic_limit=payload.semantic_limit,
                use_skills=False,
                think=payload.think,
                contrarian=False,
            )
        except OllamaError as exc:
            db.audit(
                actor="api",
                action="chat.runtime.respond",
                target=payload.model or cfg.ollama.model_for_role(payload.task_type),
                permission_tier="L0_READ",
                status="error",
                details={"error": str(exc), "task_type": payload.task_type},
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        audit_id = db.audit(
            actor="api",
            action="chat.runtime.respond",
            target=result["model"],
            permission_tier="L0_READ",
            status="ok",
            details={
                "runtime_event_id": result["event_id"],
                "model_call_id": result["model_call_id"],
                "memory_hits": len(memory_hits),
                "semantic_hits": len(semantic_hits),
                "task_type": payload.task_type,
                "governor": result["governor"],
            },
        )
        return ChatResponse(
            response=result["response"],
            model=result["model"],
            task_type=payload.task_type,
            memory_hits=memory_hits,
            semantic_hits=semantic_hits,
            audit_id=audit_id,
        )

    return app


def _model_is_installed(model: str, installed_names: set[str | None]) -> bool:
    if model in installed_names:
        return True
    if ":" not in model and f"{model}:latest" in installed_names:
        return True
    return False


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Strict, local-first CSP. `default-src 'self'` blocks every external host —
# CDNs, fonts, analytics — so nothing the browser loads or talks to can leave
# the machine. `'unsafe-inline'` is required only because the bundled UI ships
# inline <script>/<style> blocks; there is no external script origin at all.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    # blob: is required alongside 'self': the bundled dashboard (ui/index.html)
    # dynamically imports its own compiled component modules from blob: URLs
    # it creates itself (decompressed, same-origin data, never a remote
    # fetch) — without it, dynamic import() of those modules is silently
    # blocked and the dashboard never gets past its pre-hydration placeholder.
    # 'unsafe-eval' is required too: that dashboard compiles its component
    # JSX client-side via an in-browser Babel transform, which executes the
    # result through eval — there is no way to run it without eval access.
    # This does widen the XSS blast radius versus a stricter CSP, but
    # connect-src/default-src still block all external egress, so an eval'd
    # script still cannot exfiltrate data off-origin, and mutations remain
    # behind the X-Zade-Token gate regardless.
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "media-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)

_SECURITY_HEADERS = {
    "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _host_is_loopback(host: str) -> bool:
    return str(host) in _LOOPBACK_HOSTS


def _resolve_local_token(cfg: KernelConfig) -> str:
    """Return the effective mutation token, bootstrapping one if needed.

    RC1: an install left at defaults (protect_mutations on, no token configured)
    used to leave mutations wide open behind only a log warning. Instead we mint
    a random token on first boot and persist it under the kernel state dir, so
    mutations are protected-by-default and the loopback UI can auto-load it via
    /ui/session — no manual env-var step. An explicitly configured token always
    wins; turning protect_mutations off keeps the API open by choice.
    """
    if cfg.security.local_token:
        return cfg.security.local_token
    if not cfg.security.protect_mutations:
        return ""
    token_path = cfg.paths.data_dir / "local_token"
    try:
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except (FileNotFoundError, OSError):
        pass
    token = secrets.token_urlsafe(32)
    try:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(token, encoding="utf-8")
        try:  # best-effort tighten perms; harmless/no-op where unsupported (Windows)
            token_path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        # If we cannot persist it, fall back to an in-memory token for this run.
        pass
    return token


def _warn_on_weak_posture(cfg: KernelConfig, token: str) -> None:
    """Make the security posture loud instead of silently insecure.

    The kernel is safe only because it binds loopback and (by default) requires
    a mutation token. When either invariant is weakened we log a prominent
    warning so an unprotected install is never a quiet surprise.
    """
    log = logging.getLogger("cofounder_kernel.security")
    host = str(cfg.app.host)
    is_loopback = host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}  # noqa: S104 - detection, not a bind choice
    if not cfg.security.protect_mutations:
        log.warning(
            "SECURITY: protect_mutations is off — mutation endpoints are UNPROTECTED by configuration. "
            "Set [security] protect_mutations = true to require X-Zade-Token."
        )
    elif not token:
        log.warning(
            "SECURITY: protect_mutations is on but no local_token could be established (state dir unwritable?) — "
            "mutation endpoints are UNPROTECTED. Set COFOUNDER_LOCAL_TOKEN (or [security] local_token)."
        )
    if host == "0.0.0.0":  # noqa: S104 - warning about a non-loopback bind
        log.warning(
            "SECURITY: host is 0.0.0.0 — the kernel is reachable off-machine. All read endpoints are unauthenticated; "
            "bind 127.0.0.1 unless you have added transport auth."
        )
    elif not is_loopback:
        log.warning("SECURITY: host %s is not loopback; ensure this is intended and access-controlled.", host)


def _is_loopback_origin(origin: str) -> bool:
    """True only for http(s)://localhost|127.0.0.1|[::1] origins."""
    parsed = urlparse(origin)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _validated_cors_origins(cfg: KernelConfig) -> tuple[str, ...]:
    """Filter configured dev CORS origins down to the ones we'll actually allow.

    Guardrails, each with a loud SECURITY log so a misconfig is never silent:
    "*" is refused (it would open reads to every site the user visits), and any
    non-loopback origin is refused (dev CORS must never widen off-machine reads,
    since GET endpoints are unauthenticated). Returns () when nothing survives —
    which leaves CORS disabled entirely.
    """
    log = logging.getLogger("cofounder_kernel.security")
    safe: list[str] = []
    for origin in cfg.security.cors_dev_origins:
        if origin == "*":
            log.warning("SECURITY: wildcard CORS origin '*' refused; list explicit dev origins instead.")
        elif not _is_loopback_origin(origin):
            log.warning(
                "SECURITY: CORS origin %s is not loopback; refused (dev CORS must not widen off-machine reads). "
                "Remove it or add transport auth.",
                origin,
            )
        else:
            safe.append(origin)
    if safe:
        log.warning(
            "SECURITY: dev CORS is ENABLED for %s — read endpoints (including /session/token) are readable "
            "by these origins. This is a dev-only convenience; keep [security] cors_dev_origins empty in production.",
            ", ".join(safe),
        )
    return tuple(safe)


def _configure_dev_cors(app: FastAPI, cfg: KernelConfig) -> None:
    """Attach a tightly-scoped CORS layer when dev origins are configured.

    Credentials mode stays OFF: the kernel authenticates with the X-Zade-Token
    header (from localStorage), never a cookie, so no ambient credential should
    ride along cross-origin. X-Zade-Token is allow-listed so token-gated
    mutations survive the preflight. Starlette answers the OPTIONS preflight.
    """
    origins = _validated_cors_origins(cfg)
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["X-Zade-Token", "Content-Type"],
        max_age=600,
    )


def _tool_error(data: Any) -> str:
    """A clean, non-leaky error string from a failed tool result."""
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or "Request failed.")
    return "Request failed."


def _raise_build_provider_error(payload: dict[str, Any]) -> None:
    run = payload.get("run") if isinstance(payload.get("run"), dict) else payload
    if run.get("route") != "cloud":
        return
    status = str(run.get("status") or "")
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    result_status = str(result.get("status") or "")
    if result_status:
        status = result_status
    exception_type = str(result.get("exception_type") or "")
    if status == "executor_error" and exception_type in {
        "AnthropicNotConfigured",
        "AnthropicPolicyError",
    }:
        raise HTTPException(status_code=503, detail=str(result.get("error") or status)[:400])
    if status == "executor_error" and exception_type == "AnthropicError":
        raise HTTPException(status_code=502, detail=str(result.get("error") or status)[:400])
    if status not in {"model_error", "capability_error"}:
        return
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    raise HTTPException(
        status_code=502,
        detail={
            "error": str(result.get("error") or status)[:400],
            "status": status,
            "route": "cloud",
            "session_id": session.get("id"),
            "checkpoint_preserved": True,
        },
    )


def _mutation_requires_token(cfg: KernelConfig, request: Request, token: str) -> bool:
    if not token or not cfg.security.protect_mutations:
        return False
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    return True


def _security_summary(cfg: KernelConfig, token: str) -> dict[str, Any]:
    return {
        "local_only": True,
        "host": cfg.app.host,
        "port": cfg.app.port,
        "mutation_token_required": bool(token and cfg.security.protect_mutations),
        "token_bootstrapped": bool(token and not cfg.security.local_token),
        "token_header": "X-Zade-Token",
        "ui_token_storage": "localStorage.zadeKernelToken",
        "content_security_policy": True,
    }


def _founder_metrics(db: KernelDatabase) -> dict[str, Any]:
    with db.connect() as conn:
        counts = {
            "assumptions": _count(conn, "founder_assumptions"),
            "evidence": _count(conn, "founder_evidence"),
            "links": _count(conn, "founder_links"),
            "goals": _count(conn, "founder_goals"),
            "tasks": _count(conn, "founder_tasks"),
            "initiatives": _count(conn, "founder_initiatives"),
            "decisions": _count(conn, "decision_memos"),
            "active_objectives": _count(conn, "active_objectives"),
            "decision_recommendations": _count(conn, "decision_recommendations"),
            "predictions": _count(conn, "founder_predictions"),
            "experiments": _count(conn, "founder_experiments"),
            "cadence_reviews": _count(conn, "cadence_reviews"),
            "approval_training_events": _count(conn, "approval_training_events"),
        }
        calibration = conn.execute(
            """
            SELECT COUNT(*) AS scored, AVG(calibration_error) AS mean_error
            FROM founder_predictions
            WHERE calibration_error IS NOT NULL
            """
        ).fetchone()
        evidence_strength = conn.execute(
            "SELECT AVG(strength) AS average_strength FROM founder_evidence"
        ).fetchone()
    return {
        "generated_at": utc_now(),
        "counts": counts,
        "queue": db.work_queue_counts(),
        "assumptions": {
            "by_status": _count_by(db, "founder_assumptions", "status"),
        },
        "evidence": {
            "by_reliability": _count_by(db, "founder_evidence", "reliability"),
            "by_type": _count_by(db, "founder_evidence", "evidence_type"),
            "average_strength": round(float(evidence_strength["average_strength"]), 2)
            if evidence_strength and evidence_strength["average_strength"] is not None
            else None,
        },
        "predictions": {
            "by_result": _count_by(db, "founder_predictions", "result"),
            "scored_count": int(calibration["scored"] or 0) if calibration else 0,
            "mean_calibration_error": round(float(calibration["mean_error"]), 4)
            if calibration and calibration["mean_error"] is not None
            else None,
        },
        "initiatives": {
            "by_status": _count_by(db, "founder_initiatives", "status"),
            "by_risk": _count_by(db, "founder_initiatives", "current_risk"),
        },
        "experiments": {
            "by_status": _count_by(db, "founder_experiments", "status"),
        },
        "integrity": {
            "by_status": _count_by(db, "integrity_warnings", "status"),
            "by_severity": _count_by(db, "integrity_warnings", "severity"),
        },
        "approvals": {
            "by_status": _count_by(db, "approval_requests", "status"),
            "training_by_outcome": _count_by(db, "approval_training_events", "outcome"),
            "training_by_event_type": _count_by(db, "approval_training_events", "event_type"),
        },
        "models": {
            "calls_by_status": _count_by(db, "model_calls", "status"),
            "calls_by_operation": _count_by(db, "model_calls", "operation"),
        },
    }


def _count(db_conn: Any, table: str) -> int:
    row = db_conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"] if row else 0)


def _count_by(db: KernelDatabase, table: str, field: str) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {field} AS key, COUNT(*) AS count FROM {table} GROUP BY {field} ORDER BY count DESC, key ASC"
        ).fetchall()
    return {str(row["key"] or "unknown"): int(row["count"]) for row in rows}


def _build_prompt(
    message: str,
    memory_hits: list[dict[str, Any]],
    semantic_hits: list[dict[str, Any]],
    task_type: str = "general",
    assistant_name: str = "Zade",
    identity_charter: dict[str, Any] | None = None,
    relationship_charters: list[dict[str, Any]] | None = None,
    voice_charter: dict[str, Any] | None = None,
) -> str:
    memory_block = "\n".join(
        f"- [{item['kind']}] {item['title']}: {item['content'][:800]}" for item in memory_hits
    )
    if not memory_block:
        memory_block = "No relevant local memory was found."
    semantic_block = "\n".join(
        f"- [doc:{item['document_id']} chunk:{item['chunk_index']} score:{item['score']:.3f}] "
        f"{item['document_title']} ({item['source_uri']}): {item['text'][:900]}"
        for item in semantic_hits
    )
    if not semantic_block:
        semantic_block = "No relevant local document chunks were found."
    identity_block = _format_identity_charter_for_prompt(identity_charter)
    relationship_block = _format_relationship_charters_for_prompt(relationship_charters or [])
    voice_block = _format_voice_charter_for_prompt(voice_charter)
    personality_contract = _charter_personality_contract(
        {
            "identity": identity_charter or {},
            "relationships": relationship_charters or [],
            "voice": voice_charter or {},
        }
    )
    return f"""You are {assistant_name}. You are speaking as yourself, not describing an assistant persona.
Zade personality contract:
{personality_contract}

Use the founder operating layer, local memory, and semantic memory before treating the task as a one-off chat.
Use only the provided local memory unless the user asks for general reasoning.
Be direct, practical, and explicit about uncertainty.
Current model role: {task_type}.

Active runtime identity charter:
{identity_block}

Active relationship charters:
{relationship_block}

Active voice charter:
{voice_block}

Structured local memory:
{memory_block}

Semantic local document snippets:
{semantic_block}

User:
{message}
"""


def _inventory_payload(
    cfg: KernelConfig,
    authority: AuthorityPolicy,
    tools: ToolRegistry,
    db: KernelDatabase,
    founder: FounderService | None = None,
) -> dict[str, Any]:
    inventory = build_self_inventory(config=cfg, authority=authority, tools=tools.list_tools())
    inventory["identity_layer"] = {
        "routes": [
            "GET /identity/charter",
            "POST /identity/charter",
            "GET /identity/relationships",
            "GET /identity/relationships/{subject_name}",
            "POST /identity/relationships",
            "GET /identity/voice",
            "POST /identity/voice",
        ],
        "artifacts": [
            "identity_charter",
            "relationship_charters",
            "voice_charter",
        ],
        "charter_seeded": bool(founder.get_identity_charter()) if founder else False,
        "relationship_charters_active": len(founder.list_relationship_charters(status="active", limit=100)) if founder else 0,
        "voice_charter_seeded": bool(founder.get_voice_charter()) if founder else False,
    }
    inventory["work_queue"] = {
        "counts": db.work_queue_counts(),
        "routes": [
            "GET /work/queue",
            "POST /work/items",
            "POST /work/scan",
            "POST /work/run-next",
            "POST /work/run-due",
            "GET /approval-requests",
            "GET /approval-console",
            "GET /approval-console/{request_id}",
            "GET /approval-training-events",
            "GET /action-handlers",
            "POST /action-handlers/{action}/enable",
            "POST /action-handlers/{action}/disable",
            "GET /approval-requests/{request_id}",
            "POST /approval-requests/{request_id}/approve",
            "POST /approval-requests/{request_id}/deny",
            "POST /approval-requests/{request_id}/defer",
            "POST /approval-requests/{request_id}/edit",
            "POST /work/items/{item_id}/approve",
            "POST /work/items/{item_id}/deny",
            "POST /work/items/{item_id}/defer",
            "POST /work/items/{item_id}/edit",
            "POST /work/items/{item_id}/dispatch",
        ],
        "autonomous_handlers": [
            "brief.daily.prepare",
            "self.inventory.snapshot",
            "ingest.file",
            "goal.review",
        ],
        "approved_local_dispatch_handlers": [
            "local.noop",
            "local.audit.record",
            "local.memory.write",
            "local.file.write",
            "local.report.write",
            "local.vault.organize",
            "local.browser.open",
            "external.connector.sync",
            "external.browser.run",
            "local.vault.move",
            "local.vault.delete",
            "external.research.run",
            "external.dt_recommendation.ingest",
        ],
        "approval_contract": [
            "Founder direct commands are already approved without bypassing denied boundaries.",
            "Approval records founder authorization for Zade/system-proposed work without bypassing denied boundaries.",
            "The approval console exposes Zade's proposed action, evidence, risk, and authority tier before resolution.",
            "Approve, deny, defer, and edit decisions are recorded as approval_training_events for future judgment tuning.",
            "Approved work items can dispatch only when the action has a registered local handler.",
            "Function access can revoke an individual handler; a revoked handler is never dispatched even after approval.",
            "Dispatch of Zade/system-proposed approved local handlers requires the typed confirmation phrase; founder direct commands do not require a second approval phrase.",
            "Unmanaged external actions remain approved-for-record only and are not run by the kernel.",
        ],
    }
    inventory["runtime_layer"] = {
        "routes": [
            "GET /runtime/charter-stack",
            "GET /runtime/profiles",
            "GET /runtime/context",
            "POST /runtime/context",
            "POST /runtime/respond",
            "POST /runtime/operating-loop",
            "POST /runtime/evidence-loop",
            "POST /runtime/experiment-loop",
            "POST /runtime/cadence",
            "GET /runtime/events",
            "GET /models/telemetry",
            "GET /models/telemetry/calls",
            "POST /models/benchmark",
        ],
        "artifacts": [
            "runtime_events",
            "model_calls",
            "governed_responses",
            "operating_loop_runs",
            "charter_stack",
            "evidence_loop_runs",
            "experiment_loop_runs",
            "cadence_runs",
            "model_benchmarks",
        ],
        "operating_rules": [
            "Assemble identity, relationship, voice, authority, founder, memory, and queue context before responding.",
            "Prompt profile precedence is request.profile, then conversation.metadata.prompt_profile, then config.prompt_profiles.default.",
            "Source prompt tool lists are excluded from active prompts; runtime capabilities come from the local registry.",
            "Use decisive style without false certainty.",
            "Apply authority policy before implying action.",
            "Keep voice charter read-only unless explicitly updated through /identity/voice.",
            "Recommendation-shaped responses get an automatic contrarian pass through the reasoning model.",
            "Auto contrarian passes persist as contrarian reviews without touching the reply; the visible challenge block attaches only when the founder explicitly requests the contrarian pass. The draft is never silently rewritten.",
        ],
        "contrarian_pass": {
            "trigger": "Deterministic recommendation heuristic on the founder message, or the explicit contrarian request flag.",
            "model_role": "reasoning",
            "artifact": "contrarian_reviews (subject_type runtime_event)",
            "non_blocking": True,
        },
        "prompt_profiles": {
            "default": cfg.prompt_profiles.default,
            "available": [item["id"] for item in PromptProfileRegistry().list_profiles()],
        },
    }
    inventory["voice_layer"] = {
        "routes": [
            "GET /voice/status",
            "POST /voice/transcribe",
            "POST /voice/speak",
            "POST /voice/converse",
            "POST /voice/converse/stream",
        ],
        "engines": {
            "stt_configured": cfg.voice.stt_configured,
            "tts_configured": cfg.voice.tts_configured,
        },
        "operating_rules": [
            "Voice engines are founder-configured local commands run without a shell; spoken text reaches TTS via stdin, never a command line.",
            "Voice conversations run through the governed runtime: authority, charters, episodic memory, and the contrarian pass all apply.",
            "When engines are not configured, voice endpoints report unavailable instead of degrading silently.",
            "Audio and transcripts are stored under the local data dir for the audit trail.",
        ],
    }
    inventory["connector_layer"] = {
        "routes": [
            "GET /connectors",
            "POST /connectors",
            "GET /connectors/{name}",
            "POST /connectors/{name}/sync",
            "GET /connectors/items",
            "POST /connectors/items/import",
            "POST /connectors/items/{item_id}/dismiss",
        ],
        "artifacts": [
            "connectors",
            "connector_items",
        ],
        "connector_types": ["imap", "ics"],
        "operating_rules": [
            "Connectors are read-only situational awareness: IMAP mailboxes open readonly, calendars parse from exports or feeds; nothing is sent or mutated.",
            "Sync executes only through the approved dispatch flow with the typed confirmation phrase.",
            "Credentials live in environment variables referenced by name; connector configs that contain secrets are rejected.",
            "Synced items land as staged candidates and are imported as graded evidence, never as native certainty.",
        ],
    }
    inventory["research_layer"] = {
        "routes": [
            "GET /research/status",
            "GET /research/topics",
            "POST /research/daydream",
            "POST /research/run",
        ],
        "dispatch_action": "external.research.run",
        "enabled": cfg.research.enabled,
        "operating_rules": [
            "Topic derivation and the daydream are LOCAL and autonomous: they read the founder's evidence gaps and propose research questions; no network, no approval.",
            "Web fetch is the one deliberate outbound-to-the-open-web exception and runs only through approved dispatch with the typed confirmation phrase (L3 external action).",
            "Egress is https-only to public hosts, redirects refused, and byte-capped; an optional host allowlist can tighten it further.",
            "Fetched pages are salience-scored against the topic and filed as graded web_research evidence — a sourced external claim, never native certainty.",
        ],
    }
    inventory["roles_layer"] = {
        "routes": ["GET /roles", "GET /roles/status", "POST /roles/run"],
        "enabled": cfg.roles.enabled,
        "roles": ["red_team", "triage", "summarize", "gap_finder"],
        "operating_rules": [
            "The local half of the specialist swarm: each role is one governed pass on a LOCAL model — no network, no approval, no external cost.",
            "A role produces a finding attached to the subject; it never takes an action or grants permission.",
            "Findings are recorded as model-call telemetry so a role's latency and value can be measured.",
        ],
    }
    inventory["delegation_layer"] = {
        "routes": [
            "GET /delegation/status",
            "POST /delegation/brief",
            "POST /delegation/run",
            "POST /build/assess",
            "GET /build/sessions",
            "GET /build/sessions/{session_id}",
            "POST /build/sessions/{session_id}/approve",
            "POST /build/sessions/{session_id}/deny",
            "POST /build/sessions/{session_id}/run",
            "POST /build/sessions/{session_id}/plan",
            "GET /build/sessions/{session_id}/tasks",
            "POST /build/sessions/{session_id}/tasks",
            "POST /build/sessions/{session_id}/tasks/{task_id}/retry",
            "POST /build/sessions/{session_id}/run-next",
            "POST /build/sessions/{session_id}/start",
            "POST /build/sessions/{session_id}/pause",
            "POST /build/sessions/{session_id}/resume",
            "POST /build/sessions/{session_id}/cancel",
            "POST /build/sessions/{session_id}/quarantine",
            "GET /build/runs/{run_id}",
            "POST /build/runs/{run_id}/cancel",
            "GET /build/toolchains",
            "POST /build/sessions/{session_id}/verify",
            "GET /build/sessions/{session_id}/github/status",
            "GET /build/sessions/{session_id}/github/runs",
            "POST /build/sessions/{session_id}/github/dispatch",
            "POST /build/sessions/{session_id}/github/runs/{run_id}/cancel",
            "GET /build/sessions/{session_id}/review/status",
            "POST /build/sessions/{session_id}/review/prepare",
            "POST /build/sessions/{session_id}/review/approve",
            "POST /build/sessions/{session_id}/review/run",
            "GET /build/calibration",
            "POST /build/sessions/{session_id}/calibration",
            "GET /build/managed-agents/readiness",
        ],
        "dispatch_action": "external.delegation.run",
        "hybrid_assessment_action": "local.assessment.prepare",
        "enabled": cfg.delegation.enabled,
        "engine": cfg.delegation.engine,
        "build_enabled": cfg.build.enabled,
        "auto_invoke": cfg.delegation.auto_invoke,
        "daily_budget": cfg.delegation.daily_budget,
        "operating_rules": [
            "Hybrid builds assess complexity locally before any cloud client is constructed or any paid request is authorized.",
            "Routine coding, repository discovery, tools, tests, and verification stay on Zade's local Ollama coding agent.",
            "Eligible Anthropic turns require a typed project lease, lease-scoped source-code egress, and a worst-case reservation under token, dollar, turn, and time ceilings.",
            "Cloud failures never retry automatically, fall back to another paid provider, or enlarge the approved lease.",
            "Failed or interrupted local tasks may receive one operator-authorized audited retry; cloud tasks are rejected by that recovery path.",
            "Build checkpoints, immutable usage events, cache categories, route reasons, and upgrade requests survive restart in SQLite.",
            "Discovery through release is a durable local-first task graph with background start, pause, resume, cancellation, and restart recovery.",
            "All build commands are argv-only, profile-constrained, workspace-confined, time-bounded, credential-stripped, and audited.",
            "Python and Node checks prefer exact locally installed Docker images with no network; Flutter, Gradle, ADB, and emulator checks use narrow host policies.",
            "Playwright captures screenshots and traces from read-only local verification flows; required missing tools block completion.",
            "GitHub reads use the governed gh client and every workflow dispatch or cancellation requires fresh typed external-action authorization.",
            "OpenAI is optional, disabled by default, store=false, tool-free, and independently lease-budgeted for advisory review only.",
            "Managed Agents expose readiness gates only; no managed execution path exists.",
        ],
    }
    inventory["screen_layer"] = {
        "routes": ["GET /screen/status", "POST /screen/capture"],
        "enabled": cfg.screen.enabled,
        "operating_rules": [
            "Local, on-demand read of the screen: no network, no approval — but explicit, never on a timer.",
            "The textual read (focused + visible window titles) captures no pixels and needs no extra dependency.",
            "A pixel snapshot is optional (the 'screen' extra installs mss), confined to the data dir, pruned to the last N; raw pixels never cross the wire or land in a response/log.",
        ],
    }
    inventory["tray_layer"] = {
        "routes": [
            "GET /tray/state",
        ],
        "enabled": cfg.tray.enabled,
        "install_script": "scripts/install-tray-task.ps1",
        "console_script": "zade-tray",
        "operating_rules": [
            "The tray is a separate resident process (installed at logon) that polls /tray/state over loopback.",
            "It is read-only — status display and OS toasts only; it never mutates and needs no token.",
            "Status is ok/attention/error from health, pending approvals, and unread notifications; new notifications raise native toasts.",
            "GUI dependencies (pystray + Pillow) are the optional 'tray' extra; the kernel runs headless without them.",
        ],
    }
    inventory["vault_layer"] = {
        "routes": [
            "GET /vault/status",
            "GET /vault/list",
            "GET /vault/search",
            "GET /vault/trash",
            "POST /vault/move",
            "POST /vault/delete",
            "POST /vault/restore",
        ],
        "dispatch_actions": ["local.vault.move", "local.vault.delete"],
        "guard_segments": list(cfg.vault.guard_segments),
        "enabled": cfg.vault.enabled,
        "operating_rules": [
            "Reads (list/search) are direct; move and delete run only through approved dispatch with the typed confirmation phrase (L2_FILE_WRITE).",
            "Deletes and clobbered move targets go to a restorable trash snapshot under the kernel state dir — never a hard unlink.",
            "Any path segment in guard_segments (raw source-of-truth folders) is refused; a .zade-protected marker protects its whole subtree.",
            "Operating on a top-level folder needs explicit allow_top_level confirmation; a vault root can never be moved or deleted.",
            "dry_run previews the exact effect (counts, resolved paths, guard result) and changes nothing.",
        ],
    }
    inventory["browser_layer"] = {
        "routes": [
            "GET /browser/status",
            "POST /browser/run",
        ],
        "artifacts": [
            "browser_run work items",
            "browser-captures",
        ],
        "dispatch_action": "external.browser.run",
        "step_types": ["navigate", "wait", "read", "links", "fill", "click", "press", "screenshot"],
        "enabled": cfg.browser.enabled,
        "headless": cfg.browser.headless,
        "operating_rules": [
            "A browser flow is a fully-specified list of steps; the founder approves the exact sequence, which runs in one browser context.",
            "Every flow is an L3 external action: it runs only through approved dispatch with the typed confirmation phrase.",
            "Navigation is http/https only; private/internal hosts are refused unless allow_private_navigation is set.",
            "Typed values may come from a named environment variable (value_env); typed text is never written to the audit log or result.",
            "Screenshots are written only under the configured local roots, through the same path guard the file handlers use.",
        ],
    }
    inventory["eval_layer"] = {
        "routes": [
            "GET /evals/cases",
            "POST /evals/cases",
            "POST /evals/run",
            "GET /evals/runs",
            "GET /evals/runs/{run_id}",
        ],
        "artifacts": [
            "eval_cases",
            "eval_runs",
            "eval_results",
        ],
        "categories": [
            "instruction_probe",
            "critic_contract",
            "governed_contract",
            "grounding",
        ],
        "operating_rules": [
            "Eval grading is deterministic; no model judges another model's output.",
            "Each run records the active model roles so model swaps show up in run history.",
            "Every run is compared against the previous run: newly failing cases are regressions.",
            "Run evals after changing models, prompts, or routing before trusting the new configuration.",
        ],
    }
    inventory["surfacing_layer"] = {
        "routes": [
            "GET /surface/attention",
            "POST /surface/brief",
        ],
        "artifacts": [
            "initiated_briefs",
            "attention_items",
        ],
        "signal_sources": [
            "kill_criteria",
            "integrity_warnings",
            "founder_experiments",
            "thesis_conflicts",
            "founder_predictions",
            "decision_memos",
            "confidence_events",
            "founder_overrides",
            "founder_assumptions",
            "approval_requests",
            "connector_items",
            "commitments",
            "action_plans",
        ],
        "operating_rules": [
            "Attention detection is deterministic; no model call decides what needs founder attention.",
            "Initiated briefs are persisted as memories only when something needs attention.",
            "The cadence loop generates an initiated brief so Zade initiates instead of waiting to be asked.",
            "Cadence reviews include approval pressure: pending/deferred counts, the top three blockers, and the approval-console next action.",
            "Pending approval pressure can become the daily highest-leverage action until cleared, denied, deferred, or edited.",
            "Surfacing reads state; it never mutates operating objects or takes action on them.",
            "Non-quiet briefs are announced through the notification bus.",
        ],
    }
    inventory["action_pipeline_layer"] = {
        "routes": [
            "POST /action-plans",
            "POST /action-plans/from-recommendation/{recommendation_id}",
            "GET /action-plans",
            "GET /action-plans/{plan_id}",
            "POST /action-plans/{plan_id}/advance",
            "POST /action-plans/{plan_id}/steps/{step_id}/approve",
            "POST /action-plans/{plan_id}/steps/{step_id}/complete",
            "POST /action-plans/{plan_id}/steps/{step_id}/fail",
            "POST /action-plans/{plan_id}/steps/{step_id}/skip",
            "POST /action-plans/{plan_id}/steps/{step_id}/evidence",
        ],
        "artifacts": ["action_plans", "action_steps"],
        "step_statuses": ["pending", "blocked", "approval_required", "approved", "queued", "running", "done", "failed", "skipped"],
        "operating_rules": [
            "Every step carries its own authority evaluation; denied steps block the plan at creation.",
            "Machine steps execute through the work queue, so approvals and typed confirmation apply unchanged.",
            "Manual steps are founder work the pipeline tracks; it never pretends Zade executed them.",
            "Step outcomes are recorded as grade-A evidence in the founder ledger.",
        ],
    }
    inventory["devtools_layer"] = {
        "workspace_root": str(cfg.devtools.workspace_root),
        "default_branch": cfg.devtools.default_branch,
        "actions": [
            "dev.command.run",
            "dev.git.branch",
            "dev.git.commit",
            "dev.draft.write",
        ],
        "allowed_commands": sorted(allowed_commands("python").keys()),
        "operating_rules": [
            "Dev actions run only through approved dispatch: an approved work item plus the typed confirmation phrase.",
            "Commands are allowlisted (tests, lint, git diagnostics); there is no arbitrary shell execution.",
            "Execution is confined to the configured workspace root; command args cannot use absolute paths or traversal.",
            "git.commit refuses the default branch unless explicitly allowed, and only commits staged local changes.",
            "Drafts are written to the local drafts folder and never sent; sending stays a human action.",
        ],
    }
    inventory["commitment_layer"] = {
        "routes": [
            "POST /commitments",
            "GET /commitments",
            "GET /commitments/{commitment_id}",
            "POST /commitments/{commitment_id}/done",
            "POST /commitments/{commitment_id}/miss",
            "POST /commitments/{commitment_id}/drop",
            "POST /commitments/{commitment_id}/renegotiate",
            "POST /commitments/check",
        ],
        "artifacts": ["commitments", "commitment_events"],
        "operating_rules": [
            "Track what the founder said they would do and what Zade said he would monitor.",
            "The check pass flags overdue, due-soon, drifting, and monitor-due commitments; it never closes anything itself.",
            "Marking a commitment missed is an explicit founder act; history is never quietly rewritten.",
            "Repeated renegotiation is drift and gets surfaced as such.",
        ],
    }
    inventory["notification_layer"] = {
        "routes": [
            "POST /notify",
            "GET /notifications",
            "POST /notifications/{notification_id}/read",
            "GET /notify/channels",
            "POST /notify/channels/{channel}",
        ],
        "artifacts": ["notifications", "notification_deliveries", "notification_channels"],
        "channels": ["ui", "voice", "sms"],
        "operating_rules": [
            "Producers call notify(); no feature talks to a delivery channel directly.",
            "Channel rules govern egress: enabled flag, minimum severity, quiet hours, hourly rate limits, and a recipient whitelist for outbound channels.",
            "Enabling an outbound channel is a standing founder grant bounded by those rules.",
            "Critical notifications bypass quiet hours but never the whitelist or rate limit.",
            "Every suppression is recorded with its reason; nothing is dropped silently.",
        ],
    }
    inventory["conversation_layer"] = {
        "routes": [
            "POST /conversations",
            "GET /conversations",
            "GET /conversations/{conversation_id}",
            "GET /conversations/{conversation_id}/turns",
            "POST /conversations/{conversation_id}/distill",
        ],
        "artifacts": [
            "conversations",
            "conversation_turns",
        ],
        "active_conversations": len(db.list_conversations(status="active", limit=1000)),
        "operating_rules": [
            "Governed responses can carry a conversation_id to persist and recall thread continuity.",
            "Recent turns are folded into the governed prompt; older turns roll into a bounded summary.",
            "Conversation memory never overrides authority, voice charter, or evidence honesty.",
        ],
    }
    inventory["skill_layer"] = {
        "routes": [
            "GET /skills/summary",
            "GET /skills",
            "POST /skills/scan",
            "POST /skills/route",
            "GET /skills/invocations",
            "GET /skills/{name}",
            "POST /skills/{name}/enable",
            "POST /skills/{name}/disable",
        ],
        "artifacts": [
            "skill_registry",
            "skill_references",
            "skill_invocations",
            "skill_fts",
        ],
        "summary": db.skill_summary(),
        "operating_rules": [
            "Skills are retrieved as bounded procedural guidance, not as authority grants.",
            "Only enabled skills are eligible for runtime routing.",
            "External effects implied by a skill remain governed by the approval and action-handler contracts.",
            "Every runtime skill use is logged to skill_invocations.",
            "Routing blends keyword scoring with local embedding similarity; keyword routing keeps working when embeddings are unavailable.",
        ],
    }
    inventory["trading_bot_layer"] = {
        "routes": [
            "GET /trading-bot/status",
            "GET /trading-bot/safe-ops-checks",
            "GET /trading-bot/deep-thought-replacement",
            "GET /trading-bot/intelligence/access",
            "GET /trading-bot/sqlite/schema",
            "POST /trading-bot/sqlite/query",
            "POST /trading-bot/training/run",
            "GET /trading-bot/events/recent",
            "GET /trading-bot/signals/recent",
            "GET /trading-bot/market-context",
            "POST /trading-bot/evidence/snapshot",
            "POST /trading-bot/ops-check",
            "POST /trading-bot/recommendations",
            "POST /trading-bot/advisory/generate",
            "POST /trading-bot/advisory/score",
            "POST /trading-bot/daily-brief",
            "GET /trading-bot/judgments",
            "POST /trading-bot/judgments/score",
            "POST /trading-bot/dt-trigger/proposals",
        ],
        "artifacts": [
            "approval_requests",
            "work_queue",
            "approval_training_events",
            "founder_evidence",
            "memories",
            "trading_judgments",
            "missed_call_reviews",
            "read-only SQLite query audit events",
            "trading-bot training run audit events",
            "trading-bot bot_events reads",
            "trading-bot market context snapshots",
            "trading-bot signal table snapshots",
            "trading-bot evidence snapshots",
            "trading-bot dt_recommendations",
            "trading-bot dt recommendation outcome reports",
            "trading-bot direct outcome score reports",
            "Trading Project raw vault exports",
            "dt_trigger proposal records",
        ],
        "runtime_effect": "full_intelligence_no_broker_order_authority",
        "safe_write_path": "external.dt_recommendation.ingest -> scripts/dt_recommendation_ingest.py",
        "deep_thought_replacement": TradingBotBridge(config=cfg, db=db).deep_thought_replacement_map(),
        "operating_rules": [
            "Zade has full trading intelligence access for training, advisory work, events, market context, signal watching, and database visibility.",
            "Zade may run only allowlisted bot training scripts; training can write bot-owned model artifacts but cannot load models into broker/order runtime paths.",
            "Zade may run only allowlisted read-only bot diagnostics through this layer.",
            "Zade may read bot_events through the bot script or a read-only SQLite fallback.",
            "Zade may read market_context.json and daily_symbol_context snapshots.",
            "Zade may watch recent signal landing tables through read-only SQLite snapshots.",
            "Zade may query only the allowlisted trading-bot SQLite database in mode=ro with PRAGMA query_only enabled.",
            "SQLite queries are limited to SELECT, WITH, EXPLAIN, and narrow read-only PRAGMA statements; write/schema/attachment tokens are blocked before WSL execution.",
            "Evidence snapshots query only known diagnostic tables with date and optional symbol scopes.",
            "Zade may generate advisory recommendations only from real diagnostic evidence and supplied or discovered symbols.",
            "Zade may run a daily trading intelligence brief that writes only local evidence, judgment, and missed-call learning records.",
            "Zade may score judgments directly against read-only realized outcome rows without bot mutation.",
            "Zade may export an explicitly requested daily brief markdown file under the local Trading Project raw folder.",
            "Zade may queue dt_trigger proposals; approved dispatch records the proposal locally and does not run dt_trigger.",
            "Zade may propose bot advisory recommendations only as approval-gated work items.",
            "Approved dispatch appends to the bot-owned dt_recommendations lane; the bot architecture forbids runtime reads from that table.",
            "Outcome scoring runs the bot-owned read-only dt-recommendation-outcomes report and stores the score as founder evidence.",
            "The bridge does not touch broker, order, sizing, gate, execution, account-risk, or runtime decision paths.",
            "Any future promotion from advisory evidence to runtime context is a separate explicit operator decision.",
        ],
    }
    inventory["ops_layer"] = {
        "routes": [
            "GET /ops/health-check",
            "GET /ops/security",
            "GET /ops/providers",
            "GET /ops/supervision",
            "POST /ops/backup",
            "GET /ops/backups",
            "POST /ops/backups/prune",
        ],
        "artifacts": [
            "database_backups",
            "health_checks",
            "startup_smoke_logs",
            "backup_retention_audits",
            "supervision_log",
        ],
        "operating_rules": [
            "Ops endpoints inspect local posture or create local backups only.",
            "Health checks distinguish kernel/UI/Ollama readiness from cadence freshness.",
            "Provider readiness exposes cloud enablement and key presence as booleans; no secret is ever returned.",
            "Restores remain an operator script action, not an autonomous API action.",
            "The supervisor script owns the supervision log and restarts the kernel; the kernel only reads that history.",
        ],
    }
    inventory["teaching_layer"] = {
        "routes": [
            "POST /teach/deepthought/scan",
            "GET /teach/deepthought/candidates",
            "POST /teach/deepthought/import",
            "POST /teach/deepthought/link",
            "POST /teach/deepthought/auto-link",
            "GET /evidence/gaps",
        ],
        "artifacts": [
            "teaching_candidates",
            "founder_evidence",
            "documents",
            "founder_links",
            "teaching_auto_links",
        ],
        "operating_rules": [
            "Import Deep Thought material as sourced evidence, not native Zade certainty.",
            "Preserve source_system, source_uri, reliability, and entity-boundary metadata.",
            "Link evidence to assumptions, goals, bets, and predictions before treating it as operational support.",
        ],
    }
    inventory["experiment_layer"] = {
        "routes": [
            "GET /experiments",
            "POST /experiments",
            "GET /experiments/dashboard",
            "GET /experiments/reviews",
            "GET /experiments/{experiment_id}",
            "POST /experiments/{experiment_id}/evidence",
            "POST /experiments/{experiment_id}/review",
            "POST /experiments/{experiment_id}/update",
            "POST /experiments/{experiment_id}/pushback",
            "POST /runtime/experiment-loop",
            "POST /runtime/cadence",
        ],
        "artifacts": [
            "founder_experiments",
            "experiment_reviews",
            "founder_evidence",
            "founder_links",
            "contrarian_reviews",
        ],
        "operating_rules": [
            "Every experiment tests a linked assumption, bet, goal, or prediction.",
            "Experiment evidence remains in the shared founder_evidence ledger.",
            "Reviews must force one of: continue, revise, kill, or escalate.",
            "Design revisions go through the audited update route, never raw DB writes.",
            "Pushback logs disagreement without blocking execution.",
        ],
    }
    inventory["founder_operating_layer"] = {
        "routes": [
            "GET /founder/mental-models",
            "GET /founder/thesis",
            "POST /founder/thesis",
            "GET /founder/dashboard",
            "GET /founder/metrics",
            "GET /founder/brief",
            "GET /founder/active-objective",
            "GET /founder/active-objectives",
            "POST /founder/active-objectives",
            "POST /founder/active-objectives/{objective_id}/activate",
            "POST /founder/active-objectives/{objective_id}/status",
            "GET /founder/decision-recommendations",
            "GET /founder/decision-recommendations/{recommendation_id}",
            "POST /founder/decision-engine/recommend",
            "GET /founder/strategy",
            "POST /founder/strategy",
            "GET /founder/initiatives",
            "POST /founder/initiatives",
            "GET /founder/decisions",
            "POST /founder/decisions",
            "GET /founder/predictions",
            "POST /founder/predictions",
            "POST /founder/predictions/score",
            "GET /founder/contrarian-reviews",
            "POST /founder/contrarian-reviews",
            "GET /founder/reflections",
            "POST /founder/reflections",
            "GET /founder/assumptions",
            "POST /founder/assumptions",
            "GET /founder/evidence",
            "POST /founder/evidence",
            "GET /founder/links",
            "POST /founder/links",
            "GET /founder/strategy-objects",
            "POST /founder/strategy-objects",
            "GET /founder/goals",
            "POST /founder/goals",
            "GET /founder/tasks",
            "POST /founder/tasks",
            "GET /founder/kill-criteria",
            "POST /founder/kill-criteria",
            "GET /founder/overrides",
            "POST /founder/overrides",
            "GET /founder/confidence-events",
            "GET /founder/thesis-conflicts",
            "POST /founder/thesis-conflicts",
            "GET /founder/missed-calls",
            "POST /founder/missed-calls",
            "GET /founder/integrity-warnings",
            "POST /founder/integrity-check",
            "GET /founder/cadence-reviews",
            "POST /founder/cadence-reviews",
            "POST /founder/cadence-reviews/generate/{review_type}",
            "GET /experiments",
            "POST /experiments",
            "POST /runtime/experiment-loop",
        ],
        "artifacts": [
            "company_thesis",
            "strategy_ledger",
            "initiatives",
            "decision_memos",
            "predictions",
            "contrarian_reviews",
            "reflections",
            "mental_models",
            "dashboard",
            "metrics",
            "brief",
            "active_objectives",
            "decision_recommendations",
            "decision_engine_contracts",
            "assumptions",
            "evidence",
            "object_links",
            "strategy_objects",
            "goals",
            "tasks",
            "kill_criteria",
            "founder_overrides",
            "confidence_events",
            "thesis_conflicts",
            "missed_call_reviews",
            "cadence_reviews",
            "integrity_warnings",
            "experiments",
            "experiment_reviews",
        ],
        "operating_rules": [
            "One current active objective anchors Zade's default strategic focus.",
            "Decision-engine recommendations must name rationale, confidence, required evidence, downside risk, reversal condition, and next action.",
            "Recommendations may create local decision memos and founder tasks, but do not execute external actions.",
        ],
    }
    return inventory
