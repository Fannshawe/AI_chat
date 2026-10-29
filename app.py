"""FastAPI entry point for the local chat and administration pages."""

from __future__ import annotations

import json
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast
from uuid import uuid4

import yaml

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src.config import AGENT_MODEL_NAMES, GUIDANCE_DIR, STATIC_DIR, apply_agent_overrides, get_agent_model_provider, get_model_providers
from src.graph import WorkflowState
from src.graph import DEBUG, build_graph, generate_proactive_draft, node_reflection
from src.guidance import guidance_retriever
from src.prompt_loader import clear_prompt_cache
from src.storage import (
    MEMORY_CATEGORIES,
    MEMORY_OWNER_CONTACT,
    USER_MEMORY_CATEGORIES,
    RELATIONSHIP_LEVELS,
    store,
)


# ---------------------------------------------------------------------------
# Pending saves: graph results are held in memory until the user confirms send.
# Key is a monotonically increasing integer returned as `generation_id` from
# /api/chat/generate; the real DB generation_id is produced at confirm time.
# ---------------------------------------------------------------------------
_pending_lock = threading.Lock()
_pending_saves: dict[int, dict[str, Any]] = {}
_pending_counter = 0
_MAX_PENDING = 20

_generation_lock = threading.Lock()
_generation_jobs: dict[str, dict[str, Any]] = {}
_active_generation_job_id: str | None = None
_MAX_GENERATION_JOBS = 30

_NODE_STAGE_MAP: dict[str, tuple[str, int]] = {
    "context": ("读取上下文", 12),
    "emotion": ("识别情绪", 24),
    "relationship": ("关系评估", 36),
    "persona_habit_retrieval": ("检索习惯", 48),
    "memory_planner": ("记忆规划", 58),
    "memory_retrieval": ("记忆召回", 66),
    "self_emotion": ("自身情绪校准", 74),
    "guidance": ("指导检索", 80),
    "dialogue_composer": ("整合策略", 86),
    "prompt_composer": ("生成候选", 92),
    "critic": ("候选质检", 95),
    "rewrite": ("候选重写", 97),
    "reflection": ("记忆反思", 98),
    "save": ("收尾", 99),
}


def _new_draft_id() -> int:
    global _pending_counter
    with _pending_lock:
        _pending_counter += 1
        return _pending_counter


def _stage_for_node(node_name: str) -> tuple[str, int]:
    return _NODE_STAGE_MAP.get(node_name, ("处理中", 40))


def _active_job_id_locked() -> str | None:
    global _active_generation_job_id
    job_id = _active_generation_job_id
    if not job_id:
        return None
    job = _generation_jobs.get(job_id)
    if not job:
        _active_generation_job_id = None
        return None
    if job.get("status") in {"pending", "running"}:
        return job_id
    _active_generation_job_id = None
    return None


def _set_job_fields(job_id: str, **fields: Any) -> None:
    with _generation_lock:
        job = _generation_jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()


def _trim_generation_jobs_locked() -> None:
    active = _active_job_id_locked()
    if len(_generation_jobs) <= _MAX_GENERATION_JOBS:
        return
    for job_id in list(_generation_jobs.keys()):
        if len(_generation_jobs) <= _MAX_GENERATION_JOBS:
            break
        if job_id == active:
            continue
        _generation_jobs.pop(job_id, None)


def _public_job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "stage": job.get("stage", "排队中"),
        "progress": int(job.get("progress", 0)),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
        "result": job.get("result") if job.get("status") == "succeeded" else None,
    }


def _execute_generation(
    contact_id: int,
    content: str,
    progress_callback: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    initial_state = {
        "contact_id": contact_id,
        "user_request": content,
        "warnings": [],
        "debug_enabled": _debug_settings()["enabled"],
    }
    result: WorkflowState | None = None
    if progress_callback is None:
        result = cast(WorkflowState, chat_graph.invoke(initial_state))
    else:
        progress_callback("读取上下文", 6)
        for step_state in chat_graph.stream(initial_state, stream_mode="values"):
            if not isinstance(step_state, dict):
                continue
            result = cast(WorkflowState, step_state)
            traces = result.get("debug_trace", [])
            if traces:
                node_name = str((traces[-1] or {}).get("node", ""))
                stage, progress = _stage_for_node(node_name)
                progress_callback(stage, progress)
    if result is None:
        raise RuntimeError("生成流程未返回有效结果")

    draft_id = _new_draft_id()
    with _pending_lock:
        _pending_saves[draft_id] = {
            "contact_id": contact_id,
            "user_request": content,
            "history": result.get("history", []),
            "provider_name": result.get("provider_name", "default"),
            "final_candidates": result.get("candidates", []),
            "strategy": result.get("strategy", ""),
            "emotion_analysis": result.get("emotion_analysis", {}),
            "relationship_analysis": result.get("relationship_analysis", {}),
            "self_emotion_update": result.get("self_emotion_update", {}),
            "memory_plan": result.get("memory_plan", {}),
            "memories": result.get("memories", []),
            "dialogue_composition": result.get("dialogue_composition", {}),
            "critic_analysis": result.get("critic_analysis", {}),
            "reflection_analysis": result.get("reflection_analysis", {}),
            "debug_trace": result.get("debug_trace", []),
            "warnings": result.get("warnings", []),
        }
        if len(_pending_saves) > _MAX_PENDING:
            del _pending_saves[min(_pending_saves)]

    preview_message = {
        "id": None,
        "role": "user",
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "message": preview_message,
        "generation_id": draft_id,
        "strategy": result.get("strategy", ""),
        "candidates": result.get("candidates", []),
        "dialogue_plan": result.get("dialogue_composition", {}),
        "critic": result.get("critic_analysis", {}),
        "relationship": result.get("relationship", {}),
        "warnings": result.get("warnings", []),
    }


def _run_generation_job(job_id: str) -> None:
    _set_job_fields(job_id, status="running", stage="读取上下文", progress=6)
    try:
        with _generation_lock:
            job = _generation_jobs.get(job_id)
            if not job:
                return
            contact_id = int(job["contact_id"])
            content = str(job["content"])

        def _job_progress(stage: str, progress: int) -> None:
            _set_job_fields(job_id, stage=stage, progress=progress)

        result = _execute_generation(contact_id, content, progress_callback=_job_progress)
        _set_job_fields(
            job_id,
            status="succeeded",
            stage="完成",
            progress=100,
            result=result,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        _set_job_fields(
            job_id,
            status="failed",
            error=f"生成失败，请检查模型配置或服务状态：{exc}",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        global _active_generation_job_id
        with _generation_lock:
            if _active_generation_job_id == job_id:
                _active_generation_job_id = None


def _create_or_get_active_generation_job(contact_id: int, content: str) -> tuple[dict[str, Any], bool]:
    global _active_generation_job_id
    with _generation_lock:
        active_id = _active_job_id_locked()
        if active_id:
            active = _generation_jobs[active_id]
            return _public_job_view(active), False

        now = datetime.now(timezone.utc).isoformat()
        job_id = uuid4().hex
        _generation_jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "stage": "排队中",
            "progress": 0,
            "error": None,
            "result": None,
            "contact_id": contact_id,
            "content": content,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        _active_generation_job_id = job_id
        _trim_generation_jobs_locked()

    worker = threading.Thread(target=_run_generation_job, args=(job_id,), daemon=True)
    worker.start()

    with _generation_lock:
        return _public_job_view(_generation_jobs[job_id]), True


class GenerateRequest(BaseModel):
    contact_id: int
    content: str = Field(min_length=1)


class ConfirmRequest(BaseModel):
    contact_id: int
    content: str = Field(min_length=1, max_length=10000)
    generation_id: int | None = None
    candidate_index: int | None = Field(default=None, ge=0, le=2)


class MemoryRequest(BaseModel):
    contact_id: int
    category: str
    content: str = Field(min_length=1, max_length=2000)
    sensitive: bool = False


class AiMemoryProfileRequest(BaseModel):
    episodic: list[Any] = Field(default_factory=list)
    semantic: list[Any] = Field(default_factory=list)
    emotional: list[Any] = Field(default_factory=list)
    shared_experience: list[Any] = Field(default_factory=list)


class MemoryImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1_000_000)


class RelationshipRequest(BaseModel):
    level: str


class ProposalDecisionRequest(BaseModel):
    approved: bool


class ContactRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ImportMessage(BaseModel):
    role: str
    content: str
    created_at: str | None = None


class ImportRequest(BaseModel):
    contact_id: int
    text: str = ""
    messages: list[ImportMessage] = Field(default_factory=list)


class GuidanceDocumentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(max_length=1_000_000)


class ModelSelectionRequest(BaseModel):
    provider: str


class PersonaYamlRequest(BaseModel):
    text: str


class AgentModelRequest(BaseModel):
    agent: str
    model: str = Field(min_length=1, max_length=300)
    enable_thinking: bool = False


class DebugSettingsRequest(BaseModel):
    enabled: bool | None = None
    keep_generations: int | None = Field(default=None, ge=1, le=200)


def _load_agent_overrides_from_db() -> dict[str, dict]:
    """Read all per-agent model overrides that were saved to the settings table."""
    overrides: dict[str, dict] = {}
    for name in AGENT_MODEL_NAMES:
        raw = store.get_setting(f"agent_override:{name}", "")
        if raw:
            try:
                overrides[name] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return overrides


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    GUIDANCE_DIR.mkdir(parents=True, exist_ok=True)
    apply_agent_overrides(_load_agent_overrides_from_db())
    yield


app = FastAPI(title="Persona Chat", version="1.0.0", lifespan=lifespan)
chat_graph = build_graph()
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _debug_settings() -> dict[str, Any]:
    enabled = store.get_setting("debug_enabled", "true").strip().lower() in {"1", "true", "yes", "on"}
    keep_generations = int(store.get_setting("debug_keep_generations", "30") or "30")
    return {"enabled": enabled, "keep_generations": max(1, keep_generations)}


@app.get("/", include_in_schema=False)
def chat_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/debug", include_in_schema=False)
def debug_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "debug.html")


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    contacts = store.list_contacts()
    contact = contacts[0]
    contact_id = int(contact["id"])
    return {
        "contacts": contacts,
        "active_contact": contact,
        "messages": store.list_messages(contact_id, 100),
        "relationship": store.get_relationship(contact_id),
        "levels": RELATIONSHIP_LEVELS,
    }


@app.get("/api/messages/{contact_id}")
def messages(contact_id: int, limit: int = 100) -> list[dict[str, Any]]:
    return store.list_messages(contact_id, min(max(limit, 1), 500))


@app.patch("/api/contacts/{contact_id}")
def update_contact(contact_id: int, request: ContactRequest) -> dict[str, Any]:
    try:
        return store.update_contact(contact_id, request.name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/chat/generate/jobs")
def create_generate_job(request: GenerateRequest) -> dict[str, Any]:
    content = request.content.strip()
    if not content:
        raise HTTPException(400, "消息不能为空")
    job_view, accepted = _create_or_get_active_generation_job(request.contact_id, content)
    return {"accepted": accepted, **job_view}


@app.get("/api/chat/generate/jobs/active")
def get_active_generate_job() -> dict[str, Any] | None:
    with _generation_lock:
        active_id = _active_job_id_locked()
        if not active_id:
            return None
        return _public_job_view(_generation_jobs[active_id])


@app.get("/api/chat/generate/jobs/{job_id}")
def get_generate_job(job_id: str) -> dict[str, Any]:
    with _generation_lock:
        job = _generation_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return _public_job_view(job)


@app.get("/api/debug/settings")
def get_debug_settings() -> dict[str, Any]:
    return _debug_settings()


@app.put("/api/debug/settings")
def update_debug_settings(request: DebugSettingsRequest) -> dict[str, Any]:
    current = _debug_settings()
    enabled = current["enabled"] if request.enabled is None else bool(request.enabled)
    keep_generations = current["keep_generations"] if request.keep_generations is None else int(request.keep_generations)
    store.set_setting("debug_enabled", "true" if enabled else "false")
    store.set_setting("debug_keep_generations", str(keep_generations))
    return _debug_settings()


@app.post("/api/chat/confirm")
def confirm_send(request: ConfirmRequest) -> dict[str, Any]:
    """Persist everything once the user confirms they actually sent the reply."""
    pending: dict[str, Any] | None = None
    if request.generation_id is not None:
        with _pending_lock:
            pending = _pending_saves.pop(request.generation_id, None)

    if pending is None:
        # Fallback when pending data is missing (e.g. server restart).
        ai_message = store.save_message(request.contact_id, "assistant", request.content.strip())
        return {
            "assistant_message": ai_message,
            "user_message": None,
            "generation_id": None,
        }

    contact_id: int = pending["contact_id"]

    # 1. Save the other party's message.
    user_msg = store.save_message(contact_id, "user", pending["user_request"])
    input_message_id = int(user_msg["id"])

    # 2. Save emotion events.
    for item in pending["emotion_analysis"].get("emotions", [])[:4]:
        if isinstance(item, dict):
            store.add_emotion(contact_id, item)

    # 3. Apply relationship analysis.
    store.apply_relationship_analysis(contact_id, pending["relationship_analysis"])

    # 4. Save self emotion state and conversation events.
    store.save_self_emotion_state(contact_id, pending["self_emotion_update"].get("self_emotion_state", {}))
    store.add_conversation_events(contact_id, pending["self_emotion_update"].get("events", []))

    # 5. Save memories extracted by the reflection node.
    reflection = pending["reflection_analysis"]

    for item in reflection.get("memories", [])[:8]:
        if not isinstance(item, dict) or not str(item.get("content", "")).strip():
            continue
        category = str(item.get("category", "semantic"))
        if category not in USER_MEMORY_CATEGORIES:
            category = "semantic"
        store.add_memory(
            contact_id, category, str(item["content"]),
            bool(item.get("sensitive", False)), input_message_id,
            owner_type=MEMORY_OWNER_CONTACT,
        )

    # 6. Save generation record with full analysis.
    analysis = {
        "emotion": pending["emotion_analysis"],
        "relationship": pending["relationship_analysis"],
        "self_emotion": pending["self_emotion_update"],
        "memory_plan": pending["memory_plan"],
        "retrieved_memories": pending["memories"],
        "dialogue_composer": pending["dialogue_composition"],
        "critic": pending["critic_analysis"],
        "reflection": reflection,
        "warnings": pending["warnings"],
        "debug_trace": pending["debug_trace"],
    }
    generation_id = store.save_generation(
        contact_id, input_message_id, pending["strategy"], pending["final_candidates"], analysis,
    )
    if request.candidate_index is not None:
        try:
            store.select_generation_candidate(generation_id, contact_id, request.candidate_index)
        except (KeyError, ValueError):
            pass
    store.trim_generations(contact_id, _debug_settings()["keep_generations"])

    # 7. Save the AI reply.
    ai_message = store.save_message(contact_id, "assistant", request.content.strip())
    return {
        "assistant_message": ai_message,
        "user_message": user_msg,
        "generation_id": generation_id,
    }


@app.post("/api/proactive/{contact_id}")
async def proactive(contact_id: int) -> dict[str, str]:
    provider = store.get_setting("active_model_provider", "default")
    try:
        return await run_in_threadpool(generate_proactive_draft, contact_id, provider)
    except Exception as exc:
        raise HTTPException(502, f"主动聊天草稿生成失败：{exc}") from exc


@app.get("/api/persona-yaml")
def get_persona_yaml() -> dict[str, str]:
    return {"text": store.get_persona_yaml_text()}


@app.put("/api/persona-yaml")
def save_persona_yaml(request: PersonaYamlRequest) -> dict[str, str]:
    try:
        store.save_persona_yaml_text(request.text)
    except Exception as exc:
        raise HTTPException(400, f"YAML 格式错误：{exc}") from exc
    return {"text": request.text}


@app.get("/api/persona-profile")
def get_persona_profile() -> dict[str, Any]:
    return store.get_persona_profile()


@app.put("/api/persona-profile")
def save_persona_profile(request: dict[str, Any]) -> dict[str, Any]:
    return store.save_persona_profile(request)


@app.get("/api/persona-proposals/{contact_id}")
def list_persona_proposals(contact_id: int) -> list[dict[str, Any]]:
    return store.list_core_update_proposals(contact_id)


@app.post("/api/persona-proposals/{proposal_id}/resolve")
def resolve_persona_proposal(proposal_id: int, request: ProposalDecisionRequest) -> dict[str, Any]:
    try:
        return store.resolve_core_update_proposal(proposal_id, request.approved)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/memories/{contact_id}")
def list_memories(contact_id: int) -> dict[str, Any]:
    return {
        "contact_categories": USER_MEMORY_CATEGORIES,
        "ai_categories": MEMORY_CATEGORIES,
        "contact_items": store.list_memories(contact_id, owner_type=MEMORY_OWNER_CONTACT),
        "ai_profile": store.get_ai_memory_profile(),
    }


@app.get("/api/ai-memory")
def get_ai_memory_profile() -> dict[str, Any]:
    return store.get_ai_memory_profile()


@app.put("/api/ai-memory")
def save_ai_memory_profile(request: AiMemoryProfileRequest) -> dict[str, Any]:
    return store.save_ai_memory_profile(request.model_dump())


@app.post("/api/memories")
def add_memory(request: MemoryRequest) -> dict[str, Any]:
    if request.category not in USER_MEMORY_CATEGORIES:
        raise HTTPException(400, "未知记忆分类")
    return store.add_memory(
        request.contact_id,
        request.category,
        request.content,
        request.sensitive,
        owner_type=MEMORY_OWNER_CONTACT,
    )


def _parse_memory_import(text: str, categories: list[str]) -> list[dict[str, Any]]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(400, "记忆文件不是有效的 YAML/JSON") from exc
    if isinstance(data, dict):
        if not data or any(category not in categories for category in data):
            raise HTTPException(400, "记忆文件包含未知分类")
        records = []
        for category, items in data.items():
            if not isinstance(items, list):
                raise HTTPException(400, "每个记忆分类必须是列表")
            for item in items:
                if isinstance(item, str):
                    item = {"content": item}
                if not isinstance(item, dict):
                    raise HTTPException(400, "记忆项必须是文本或对象")
                records.append({**item, "category": category})
    elif isinstance(data, list):
        records = data
    else:
        raise HTTPException(400, "记忆文件必须是分类对象或记录列表")
    normalized = []
    for item in records:
        if not isinstance(item, dict) or item.get("category") not in categories:
            raise HTTPException(400, "记忆项缺少有效分类")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip() or len(content.strip()) > 2000:
            raise HTTPException(400, "记忆内容必须是 1 至 2000 字的文本")
        sensitive = item.get("sensitive", False)
        if not isinstance(sensitive, (bool, int)) or sensitive not in (0, 1):
            raise HTTPException(400, "sensitive 必须是布尔值或 0/1")
        normalized.append({**item, "content": content.strip(), "sensitive": bool(sensitive)})
    if not normalized:
        raise HTTPException(400, "文件中没有可导入的记忆")
    return normalized


@app.post("/api/ai-memory/import")
def import_ai_memory(request: MemoryImportRequest) -> dict[str, int]:
    records = _parse_memory_import(request.text, MEMORY_CATEGORIES)
    profile = store.get_ai_memory_profile()
    imported = 0
    for item in records:
        category = item["category"]
        contents = {
            entry.get("content", "").strip() if isinstance(entry, dict) else str(entry).strip()
            for entry in profile[category]
        }
        if item["content"] in contents:
            continue
        profile[category].append({
            key: value for key, value in item.items()
            if key not in {"category", "id", "contact_id", "owner_id", "owner_type", "source_message_id"}
        })
        imported += 1
    if imported:
        store.save_ai_memory_profile(profile)
    return {"imported": imported, "skipped": len(records) - imported}


@app.post("/api/memories/{contact_id}/import")
def import_contact_memory(contact_id: int, request: MemoryImportRequest) -> dict[str, int]:
    if not any(contact["id"] == contact_id for contact in store.list_contacts()):
        raise HTTPException(404, "联系人不存在")
    records = _parse_memory_import(request.text, USER_MEMORY_CATEGORIES)
    source_contacts = {str(item["contact_id"]) for item in records if item.get("contact_id") is not None}
    if len(source_contacts) > 1:
        raise HTTPException(400, "文件包含多个联系人的记忆，请分别导入")
    if any(item.get("owner_type", MEMORY_OWNER_CONTACT) != MEMORY_OWNER_CONTACT for item in records):
        raise HTTPException(400, "Contact Memory 文件不能包含 AI 记忆")
    imported = 0
    for item in records:
        with store.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM memories WHERE contact_id=? AND owner_type=? AND category=? AND content=?",
                (contact_id, MEMORY_OWNER_CONTACT, item["category"], item["content"]),
            ).fetchone()
        if existing is not None:
            continue
        store.add_memory(contact_id, item["category"], item["content"], item["sensitive"],
                         owner_type=MEMORY_OWNER_CONTACT)
        imported += 1
    return {"imported": imported, "skipped": len(records) - imported}


@app.put("/api/memories/{memory_id}")
def update_memory(memory_id: int, request: MemoryRequest) -> dict[str, Any]:
    if request.category not in USER_MEMORY_CATEGORIES:
        raise HTTPException(400, "未知记忆分类")
    try:
        return store.update_memory(memory_id, request.category, request.content, request.sensitive)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: int) -> None:
    store.delete_memory(memory_id)


@app.get("/api/emotions/{contact_id}")
def list_emotions(contact_id: int) -> list[dict[str, Any]]:
    return store.list_emotions(contact_id)


@app.post("/api/emotions/{emotion_id}/confirm", status_code=204)
def confirm_emotion(emotion_id: int) -> None:
    store.confirm_emotion(emotion_id)


@app.get("/api/relationship/{contact_id}")
def get_relationship(contact_id: int) -> dict[str, Any]:
    return store.get_relationship(contact_id)


@app.get("/api/relationship/{contact_id}/events")
def relationship_events(contact_id: int, limit: int = 100) -> list[dict[str, Any]]:
    return store.list_relationship_events(contact_id, min(max(limit, 1), 500))


@app.put("/api/relationship/{contact_id}")
def update_relationship(contact_id: int, request: RelationshipRequest) -> dict[str, Any]:
    try:
        return store.set_relationship(contact_id, request.level)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/guidance/documents")
def guidance_documents() -> list[dict[str, Any]]:
    return guidance_retriever.list_documents()


@app.post("/api/guidance/documents")
def save_guidance_document(request: GuidanceDocumentRequest) -> dict[str, Any]:
    safe_name = Path(request.name).name
    if not safe_name.lower().endswith(".md"):
        safe_name += ".md"
    path = GUIDANCE_DIR / safe_name
    path.write_text(request.content, encoding="utf-8")
    clear_prompt_cache()
    return {"name": safe_name, "size": path.stat().st_size}


@app.get("/api/guidance/documents/{name}")
def read_guidance_document(name: str) -> dict[str, str]:
    safe_name = Path(name).name
    path = GUIDANCE_DIR / safe_name
    if not path.exists() or path.suffix.lower() != ".md":
        raise HTTPException(404, "文档不存在")
    return {"name": safe_name, "content": path.read_text(encoding="utf-8")}


@app.delete("/api/guidance/documents/{name}", status_code=204)
def delete_guidance_document(name: str) -> None:
    safe_name = Path(name).name
    path = GUIDANCE_DIR / safe_name
    if path.exists() and path.suffix.lower() == ".md":
        path.unlink()


def _parse_import_text(text: str) -> list[ImportMessage]:
    parsed: list[ImportMessage] = []
    pattern = re.compile(r"^\s*([^:：]{1,20})[:：]\s*(.+)$")
    self_labels = {"我", "自己", "me", "self"}
    other_labels = {"对方", "她", "他", "them", "other"}
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        speaker, content = match.groups()
        label = speaker.strip().lower()
        if label in self_labels:
            role = "assistant"
        elif label in other_labels:
            role = "user"
        else:
            continue
        parsed.append(ImportMessage(role=role, content=content.strip()))
    return parsed


@app.post("/api/import")
def import_chat(request: ImportRequest) -> dict[str, Any]:
    if not any(int(contact["id"]) == request.contact_id for contact in store.list_contacts()):
        raise HTTPException(404, "联系人不存在")
    records = request.messages or _parse_import_text(request.text)
    messages: list[dict[str, Any]] = []
    for item in records:
        if item.role not in {"user", "assistant"} or not item.content.strip():
            continue
        messages.append(store.save_message(
            request.contact_id, item.role, item.content.strip(), created_at=item.created_at,
        ))

    memories_added = 0
    warnings: list[str] = []
    provider_name = store.get_setting("active_model_provider", "default")
    for offset in range(0, len(messages), 6):
        batch = messages[offset:offset + 6]
        user_messages = [message for message in batch if message["role"] == "user"]
        if not user_messages:
            continue
        state: WorkflowState = {
            "contact_id": request.contact_id,
            "user_request": "\n".join(message["content"] for message in user_messages),
            "relationship": {},
            "candidates": [],
            "provider_name": provider_name,
            "history": messages[max(0, offset - 2):offset + 6],
            "memories": store.search_memories(
                request.contact_id, "\n".join(message["content"] for message in user_messages), limit=6,
            ),
        }
        result = node_reflection(state)
        warnings.extend(result.get("warnings", []))
        for item in result.get("reflection_analysis", {}).get("memories", [])[:8]:
            if not isinstance(item, dict) or not str(item.get("content", "")).strip():
                continue
            category = str(item.get("category", "semantic"))
            if category not in USER_MEMORY_CATEGORIES:
                category = "semantic"
            content = str(item["content"]).strip()
            with store.connect() as connection:
                existing = connection.execute(
                    "SELECT id FROM memories WHERE contact_id = ? AND owner_type = ? AND category = ? AND content = ?",
                    (request.contact_id, MEMORY_OWNER_CONTACT, category, content),
                ).fetchone()
            store.add_memory(
                request.contact_id, category, content, bool(item.get("sensitive", False)),
                int(user_messages[-1]["id"]), owner_type=MEMORY_OWNER_CONTACT,
            )
            if existing is None:
                memories_added += 1
    return {"imported": len(messages), "memories_added": memories_added, "warnings": warnings}


@app.get("/api/export")
def export_data() -> JSONResponse:
    store.export_yaml_files()
    return JSONResponse(
        store.export_all(),
        headers={"Content-Disposition": "attachment; filename=persona-chat-export.json"},
    )


@app.get("/api/models")
def models() -> dict[str, Any]:
    providers = get_model_providers()
    active = store.get_setting("active_model_provider", "default")
    if active not in providers:
        active = "default"
    return {"active": active, "providers": [provider.public_dict() for provider in providers.values()]}


@app.get("/api/debug/generations")
def debug_generations(contact_id: int, limit: int = 20) -> list[dict[str, Any]]:
    items = store.list_generations(contact_id, min(max(limit, 1), 100))
    return [{
        "id": item["id"],
        "created_at": item["created_at"],
        "strategy": item.get("strategy", ""),
        "selected_candidate_index": item.get("selected_candidate_index"),
        "candidate_count": len(item.get("candidates", [])),
        "warning_count": len(item.get("analysis", {}).get("warnings", [])),
        "trace_count": len(item.get("analysis", {}).get("debug_trace", [])),
    } for item in items]


@app.get("/api/debug/generations/{generation_id}")
def debug_generation_detail(generation_id: int) -> dict[str, Any]:
    for contact in store.list_contacts():
        for item in store.list_generations(int(contact["id"]), 100):
            if int(item["id"]) == generation_id:
                analysis = item.get("analysis", {})
                return {
                    "debug": {"enabled": DEBUG, "detail_enabled": DEBUG},
                    "generation": item,
                    "debug_trace": analysis.get("debug_trace", []),
                    "warnings": analysis.get("warnings", []),
                }
    raise HTTPException(404, "调试记录不存在")


@app.get("/api/agent-models")
def agent_models_list() -> list[dict[str, Any]]:
    """Return effective model config (env + DB overrides) for every agent."""
    result: list[dict[str, Any]] = []
    for name in AGENT_MODEL_NAMES:
        try:
            provider = get_agent_model_provider(name)
            result.append({
                "agent": name,
                "model": provider.model,
                "enable_thinking": bool(provider.enable_thinking),
                "base_url": provider.base_url,
                "configured": bool(provider.api_key),
            })
        except Exception as exc:  # noqa: BLE001
            result.append({
                "agent": name,
                "model": "",
                "enable_thinking": False,
                "base_url": None,
                "configured": False,
                "error": str(exc),
            })
    return result


@app.put("/api/agent-models")
def update_agent_model(request: AgentModelRequest) -> dict[str, Any]:
    """Persist a per-agent model override and reload in-memory config."""
    from src.agents import clear_agent_cache
    from src.llm_service import clear_model_cache

    if request.agent not in AGENT_MODEL_NAMES:
        raise HTTPException(400, "未知 Agent 名称")

    override = {"model": request.model, "enable_thinking": request.enable_thinking}
    store.set_setting(f"agent_override:{request.agent}", json.dumps(override))

    # Re-apply all overrides so the in-memory dict is consistent
    apply_agent_overrides(_load_agent_overrides_from_db())

    # Clear model/agent caches so the next invocation uses the new settings
    clear_model_cache()
    clear_agent_cache()

    return {"agent": request.agent, "model": request.model, "enable_thinking": request.enable_thinking}


@app.put("/api/models")
def select_model(request: ModelSelectionRequest) -> dict[str, str]:
    if request.provider not in get_model_providers():
        raise HTTPException(400, "模型供应商不存在")
    store.set_setting("active_model_provider", request.provider)
    return {"active": request.provider}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
