from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .approval import ApprovalService
from .authority import AuthorityPolicy, AuthorityRequest, build_self_inventory
from .autonomy import WorkQueueService
from .brief import build_daily_brief
from .config import KernelConfig, ensure_local_paths, load_config
from .connectors import ConnectorService
from .conversation import ConversationService
from .critic import ContrarianCritic
from .db import KernelDatabase, utc_now
from .devtools import DevToolsHandlers, allowed_commands
from .evals import EvalService
from .experiments import ExperimentService
from .founder import FounderService
from .handlers import ActionHandlerRegistry
from .ingestion import IngestionService
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
    AuthorityEvaluateRequest,
    BackupCreateRequest,
    BackupRetentionRequest,
    CadenceReviewCreate,
    CadenceRunRequest,
    ChatRequest,
    ChatResponse,
    CommitmentClose,
    CommitmentCreate,
    CommitmentRenegotiate,
    CompanyThesisUpsert,
    ConnectorCreate,
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
from .actions import ActionPipelineService
from .commitments import CommitmentLedger
from .notify import NotificationBus
from .runtime import RuntimeService
from .skills import SkillService
from .surfacing import SurfacingService
from .voice import VoiceNotConfigured, VoiceService
from .teaching import DeepThoughtTeachingBridge
from .trading_bot import TradingBotBridge
from .tools import ToolRegistry


UI_DIR = Path(__file__).resolve().parents[2] / "ui"


def create_app(config: KernelConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    ensure_local_paths(cfg)

    db = KernelDatabase(cfg.paths.database_path)
    db.migrate()
    ollama = OllamaClient(cfg.ollama)
    authority = AuthorityPolicy.from_config(cfg)
    tools = ToolRegistry(db, authority=authority)
    ingestion = IngestionService(config=cfg, db=db, embedder=ollama)
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
    conversations = ConversationService(config=cfg, db=db, ollama=ollama)
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
    )
    teaching = DeepThoughtTeachingBridge(config=cfg, db=db, founder=founder, ingestion=ingestion)
    experiments = ExperimentService(config=cfg, db=db, founder=founder, ingestion=ingestion)
    connectors = ConnectorService(config=cfg, db=db, founder=founder, ingestion=ingestion, work_queue=work_queue)
    handlers.register(
        "external.connector.sync",
        "Read-only sync of an approved external connector into staged candidate items.",
        connectors.sync_from_work_item,
    )
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
    bus = NotificationBus(db=db, voice=voice)
    commitments = CommitmentLedger(db=db, bus=bus)
    actions = ActionPipelineService(db=db, authority=authority, founder=founder, work_queue=work_queue, bus=bus)
    surfacing = SurfacingService(config=cfg, db=db, ollama=ollama, bus=bus)

    app = FastAPI(title=f"{cfg.identity.name} Local AI Co-founder Kernel", version="0.1.0")
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
    app.state.config = cfg
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
    app.state.surfacing = surfacing
    app.state.bus = bus
    app.state.commitments = commitments
    app.state.actions = actions
    app.state.ops = ops
    app.state.evals = evals
    app.state.voice = voice

    @app.middleware("http")
    async def local_mutation_guard(request: Request, call_next):
        if _mutation_requires_token(cfg, request):
            supplied = request.headers.get("x-zade-token", "")
            if supplied != cfg.security.local_token:
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": "Local mutation token required.",
                        "hint": "Set localStorage.zadeKernelToken in /ui or send X-Zade-Token.",
                    },
                )
        return await call_next(request)

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
            "work_queue": db.work_queue_counts(),
            "authority": {
                "policy_version": authority.summary()["policy_version"],
                "typed_confirmation_phrase": authority.summary()["typed_confirmation_phrase"],
            },
            "security": _security_summary(cfg),
            "skills": db.skill_summary(),
            "tools": tools.list_tools(),
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
        return _security_summary(cfg)

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

    @app.get("/runtime/context")
    def runtime_context(
        message: str = "",
        task_type: str = "general",
        use_memory: bool = True,
        use_semantic_memory: bool = True,
        semantic_limit: int = 4,
        use_skills: bool = True,
        skill_limit: int = 3,
    ) -> dict[str, Any]:
        return runtime.context(
            message=message,
            task_type=task_type,  # type: ignore[arg-type]
            use_memory=use_memory,
            use_semantic_memory=use_semantic_memory,
            semantic_limit=semantic_limit,
            use_skills=use_skills,
            skill_limit=skill_limit,
        )

    @app.post("/runtime/context")
    def runtime_context_post(payload: RuntimeContextRequest) -> dict[str, Any]:
        return runtime.context(**payload.model_dump())

    @app.post("/runtime/respond")
    def runtime_respond(payload: RuntimeRespondRequest) -> dict[str, Any]:
        try:
            return runtime.respond(**payload.model_dump())
        except OllamaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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

    @app.get("/trading-bot/status")
    def trading_bot_status() -> dict[str, Any]:
        return trading_bot.status()

    @app.get("/trading-bot/safe-ops-checks")
    def trading_bot_safe_ops_checks() -> dict[str, Any]:
        return {"items": trading_bot.safe_ops_checks()}

    @app.get("/trading-bot/deep-thought-replacement")
    def trading_bot_deep_thought_replacement() -> dict[str, Any]:
        return trading_bot.deep_thought_replacement_map()

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
            raise HTTPException(status_code=400, detail=result.data)
        return result.data

    @app.post("/memory/search")
    def search_memory(payload: MemorySearch) -> dict[str, Any]:
        result = tools.call("memory.search", payload.model_dump(), actor="api")
        if not result.ok:
            raise HTTPException(status_code=400, detail=result.data)
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
            raise HTTPException(status_code=400, detail=result.__dict__)
        return result.__dict__

    @app.post("/ingest/file")
    def ingest_file(payload: IngestFileRequest) -> dict[str, Any]:
        result = ingestion.ingest_file(path=payload.path, metadata=payload.metadata)
        if result.status == "error":
            raise HTTPException(status_code=400, detail=result.__dict__)
        return result.__dict__

    @app.post("/ingest/folder")
    def ingest_folder(payload: IngestFolderRequest) -> dict[str, Any]:
        result = ingestion.ingest_folder(path=payload.path, recursive=payload.recursive, metadata=payload.metadata)
        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result)
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
            raise HTTPException(status_code=400, detail=result.data)
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
            memory_hits = [record.__dict__ for record in db.search_memories(payload.message, limit=5)]
        if payload.use_memory and payload.use_semantic_memory and payload.semantic_limit > 0:
            try:
                semantic_hits = ingestion.semantic_search(query=payload.message, limit=payload.semantic_limit)
            except OllamaError:
                semantic_hits = []

        selected_model = payload.model or cfg.ollama.model_for_role(payload.task_type)
        identity_charter = founder.get_identity_charter()
        relationship_charters = founder.list_relationship_charters(status="active", limit=5)
        voice_charter = founder.get_voice_charter()
        prompt = _build_prompt(
            payload.message,
            memory_hits,
            semantic_hits,
            payload.task_type,
            assistant_name=cfg.identity.name,
            identity_charter=identity_charter,
            relationship_charters=relationship_charters,
            voice_charter=voice_charter,
        )
        try:
            think = payload.think if payload.think is not None else cfg.ollama.think_for_role(payload.task_type)
            result = ollama.generate(
                prompt=prompt,
                model=selected_model,
                think=think,
                temperature=cfg.ollama.temperature,
            )
        except OllamaError as exc:
            db.audit(
                actor="api",
                action="model.generate",
                target=selected_model,
                permission_tier="L0_READ",
                status="error",
                details={"error": str(exc), "task_type": payload.task_type},
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        audit_id = db.audit(
            actor="api",
            action="model.generate",
            target=result.model,
            permission_tier="L0_READ",
            status="ok",
            details={
                "memory_hits": len(memory_hits),
                "semantic_hits": len(semantic_hits),
                "task_type": payload.task_type,
                "think": think,
                "identity_charter": bool(identity_charter),
                "relationship_charters": len(relationship_charters),
                "voice_charter": bool(voice_charter),
            },
        )
        return ChatResponse(
            response=result.response,
            model=result.model,
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


def _mutation_requires_token(cfg: KernelConfig, request: Request) -> bool:
    if not cfg.security.local_token or not cfg.security.protect_mutations:
        return False
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    return True


def _security_summary(cfg: KernelConfig) -> dict[str, Any]:
    return {
        "local_only": True,
        "host": cfg.app.host,
        "port": cfg.app.port,
        "mutation_token_required": bool(cfg.security.local_token and cfg.security.protect_mutations),
        "token_header": "X-Zade-Token",
        "ui_token_storage": "localStorage.zadeKernelToken",
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
    return f"""You are {assistant_name}, a local-first AI co-founder kernel.
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


def _format_identity_charter_for_prompt(identity_charter: dict[str, Any] | None) -> str:
    if not identity_charter:
        return "No runtime identity charter has been seeded. Use the default local co-founder posture."

    def item_text(item: Any) -> str:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("principle") or item.get("risk") or item.get("trait") or "").strip()
            rule = str(item.get("rule") or item.get("description") or item.get("mitigation") or "").strip()
            return f"{name}: {rule}".strip(": ")
        return str(item).strip()

    def list_block(label: str, values: list[Any], limit: int = 6) -> list[str]:
        items = [item_text(item) for item in values if item_text(item)]
        if not items:
            return []
        return [f"- {label}: " + "; ".join(items[:limit])]

    safety = identity_charter.get("safety_translation") or {}
    safety_items = [f"{key} maps to {value}" for key, value in safety.items()]
    lines = [
        f"- Name: {identity_charter.get('name', 'Zade')}",
        f"- Mission: {identity_charter.get('mission', '') or 'Operate as a local-first AI co-founder.'}",
        *list_block("Guiding principles", identity_charter.get("guiding_principles", []), limit=5),
        *list_block("Cognitive style", identity_charter.get("cognitive_style", []), limit=6),
        *list_block("Communication style", identity_charter.get("communication_style", []), limit=5),
        *list_block("Decision framework", identity_charter.get("decision_framework", []), limit=6),
        *list_block("Risk controls", identity_charter.get("risk_controls", []), limit=5),
    ]
    if safety_items:
        lines.append("- Safety translation: " + "; ".join(safety_items[:6]))
    lines.append("- Boundary: Follow the authority policy. Never coerce, threaten, stalk, harass, or cause harm.")
    return "\n".join(line for line in lines if line.strip())


def _format_relationship_charters_for_prompt(charters: list[dict[str, Any]]) -> str:
    active = [item for item in charters if item.get("status", "active") == "active"]
    if not active:
        return "No active relationship charters have been seeded."
    blocks = []
    for charter in active[:5]:
        safety = charter.get("safety_translation") or {}
        safety_items = [f"{key} maps to {value}" for key, value in safety.items()]
        boundaries = [str(item) for item in charter.get("boundaries", []) if str(item).strip()]
        risk_controls = []
        for item in charter.get("risk_controls", []):
            if isinstance(item, dict):
                risk = str(item.get("risk", "")).strip()
                mitigation = str(item.get("mitigation", "")).strip()
                risk_controls.append(f"{risk}: {mitigation}".strip(": "))
            else:
                risk_controls.append(str(item))
        lines = [
            f"- Subject: {charter.get('subject_name', 'unknown')} ({charter.get('relationship_type', 'protected_principal')})",
            f"- First principle: {charter.get('first_principle', '')}",
        ]
        if safety_items:
            lines.append("- Safe translation: " + "; ".join(safety_items[:6]))
        if boundaries:
            lines.append("- Boundaries: " + "; ".join(boundaries[:6]))
        if risk_controls:
            lines.append("- Risk controls: " + "; ".join(risk_controls[:5]))
        lines.append("- Boundary: Care never authorizes surveillance, coercion, possessive control, harassment, or harm.")
        blocks.append("\n".join(line for line in lines if line.strip()))
    return "\n\n".join(blocks)


def _format_voice_charter_for_prompt(voice_charter: dict[str, Any] | None) -> str:
    if not voice_charter:
        return "No active voice charter has been seeded. Use the default direct co-founder voice."

    def text_list(values: Any, limit: int = 6) -> list[str]:
        if isinstance(values, list):
            return [str(item).strip() for item in values[:limit] if str(item).strip()]
        return []

    vocabulary = voice_charter.get("vocabulary") or {}
    sentence = voice_charter.get("sentence_structure") or {}
    rhythm = voice_charter.get("rhythm") or {}
    confidence = voice_charter.get("confidence_style") or {}
    threats = voice_charter.get("threat_translation") or {}
    uncertainty = voice_charter.get("uncertainty_policy") or {}
    controls = []
    for item in voice_charter.get("safety_controls", []):
        if isinstance(item, dict):
            control = str(item.get("control") or item.get("risk") or "").strip()
            rule = str(item.get("rule") or item.get("mitigation") or "").strip()
            controls.append(f"{control}: {rule}".strip(": "))
        else:
            controls.append(str(item))
    preferred_words = text_list(vocabulary.get("preferred_words", []), limit=10)
    avoid_words = text_list(vocabulary.get("avoid_words", []), limit=8)
    lines = [
        f"- Name: {voice_charter.get('name', 'Zade')}",
        f"- Overall: {voice_charter.get('overall_voice', '')}",
        f"- Sentence structure: {sentence.get('rule', 'Mostly short, direct sentences.')}",
        f"- Rhythm: {rhythm.get('rule', 'Short statements, then a longer decisive sentence when needed.')}",
        f"- Confidence: {confidence.get('rule', 'Sound decisive, but never fake certainty.')}",
        f"- Uncertainty: {uncertainty.get('rule', 'State what is known, what is missing, and the next check without hedging.')}",
    ]
    if preferred_words:
        lines.append("- Preferred words: " + ", ".join(preferred_words))
    if avoid_words:
        lines.append("- Avoid filler: " + ", ".join(avoid_words))
    if threats:
        lines.append("- Threat translation: " + "; ".join(f"{key} maps to {value}" for key, value in threats.items()))
    if controls:
        lines.append("- Safety controls: " + "; ".join(controls[:6]))
    lines.append("- Boundary: Do not issue real threats, coercive commands, harassment, violent imagery, or false certainty.")
    return "\n".join(line for line in lines if line.strip())


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
            "external.dt_recommendation.ingest",
        ],
        "approval_contract": [
            "Approval records founder authorization without bypassing denied boundaries.",
            "The approval console exposes Zade's proposed action, evidence, risk, and authority tier before resolution.",
            "Approve, deny, defer, and edit decisions are recorded as approval_training_events for future judgment tuning.",
            "Approved work items can dispatch only when the action has a registered local handler.",
            "Dispatch of approved local handlers requires the typed confirmation phrase.",
            "Unmanaged external actions remain approved-for-record only and are not run by the kernel.",
        ],
    }
    inventory["runtime_layer"] = {
        "routes": [
            "GET /runtime/charter-stack",
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
            "Use decisive style without false certainty.",
            "Apply authority policy before implying action.",
            "Keep voice charter read-only unless explicitly updated through /identity/voice.",
            "Recommendation-shaped responses get an automatic contrarian pass through the reasoning model.",
            "Contrarian challenges attach visibly to the response and persist as contrarian reviews; they never silently rewrite the draft.",
        ],
        "contrarian_pass": {
            "trigger": "Deterministic recommendation heuristic on the founder message, or the explicit contrarian request flag.",
            "model_role": "reasoning",
            "artifact": "contrarian_reviews (subject_type runtime_event)",
            "non_blocking": True,
        },
    }
    inventory["voice_layer"] = {
        "routes": [
            "GET /voice/status",
            "POST /voice/transcribe",
            "POST /voice/speak",
            "POST /voice/converse",
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
            "GET /trading-bot/sqlite/schema",
            "POST /trading-bot/sqlite/query",
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
            "trading-bot evidence snapshots",
            "trading-bot dt_recommendations",
            "trading-bot dt recommendation outcome reports",
            "trading-bot direct outcome score reports",
            "Trading Project raw vault exports",
            "dt_trigger proposal records",
        ],
        "runtime_effect": "advisory_only_no_trade_authority",
        "safe_write_path": "external.dt_recommendation.ingest -> scripts/dt_recommendation_ingest.py",
        "deep_thought_replacement": TradingBotBridge(config=cfg, db=db).deep_thought_replacement_map(),
        "operating_rules": [
            "Zade may run only allowlisted read-only bot diagnostics through this layer.",
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
