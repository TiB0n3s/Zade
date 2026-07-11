from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from .authority import AuthorityPolicy, AuthorityRequest, build_self_inventory
from .autonomy import WorkQueueService
from .brief import build_daily_brief
from .config import KernelConfig, ensure_local_paths, load_config
from .db import KernelDatabase
from .founder import FounderService
from .ingestion import IngestionService
from .models import (
    AssumptionCreate,
    AuthorityEvaluateRequest,
    CadenceReviewCreate,
    ChatRequest,
    ChatResponse,
    CompanyThesisUpsert,
    ContrarianReviewCreate,
    DecisionMemoCreate,
    EvidenceCreate,
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
    ObjectLinkCreate,
    ReflectionCreate,
    RelationshipCharterUpsert,
    SemanticSearchRequest,
    StrategyEntryCreate,
    StrategyObjectCreate,
    ThesisConflictCreate,
    VoiceCharterUpsert,
    WorkItemCreate,
    WorkRunRequest,
    WorkScanRequest,
)
from .ollama import OllamaClient, OllamaError
from .tools import ToolRegistry


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
    work_queue = WorkQueueService(
        config=cfg,
        db=db,
        authority=authority,
        ingestion=ingestion,
        inventory_provider=lambda: _inventory_payload(cfg, authority, tools, db, founder),
    )

    app = FastAPI(title=f"{cfg.identity.name} Local AI Co-founder Kernel", version="0.1.0")
    app.state.config = cfg
    app.state.db = db
    app.state.ollama = ollama
    app.state.authority = authority
    app.state.tools = tools
    app.state.ingestion = ingestion
    app.state.founder = founder
    app.state.work_queue = work_queue

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

    @app.get("/founder/brief")
    def founder_brief() -> dict[str, Any]:
        return founder.brief()

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
            matches = ingestion.semantic_search(query=payload.query, limit=payload.limit)
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
        ],
        "autonomous_handlers": [
            "brief.daily.prepare",
            "self.inventory.snapshot",
            "ingest.file",
            "goal.review",
        ],
    }
    inventory["founder_operating_layer"] = {
        "routes": [
            "GET /founder/mental-models",
            "GET /founder/thesis",
            "POST /founder/thesis",
            "GET /founder/dashboard",
            "GET /founder/brief",
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
            "brief",
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
        ],
    }
    return inventory
