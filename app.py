"""FastAPI entry point for the local chat and administration pages."""

from __future__ import annotations

import base64
import binascii
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src.config import GUIDANCE_DIR, MAX_IMAGE_BYTES, STATIC_DIR, UPLOAD_DIR, get_model_providers
from src.graph import build_graph, generate_proactive_draft
from src.guidance import guidance_retriever
from src.prompt_loader import clear_prompt_cache
from src.storage import MEMORY_CATEGORIES, RELATIONSHIP_LEVELS, store


class ImageInput(BaseModel):
    name: str = "image"
    mime: str
    data: str


class GenerateRequest(BaseModel):
    contact_id: int
    content: str = ""
    images: list[ImageInput] = Field(default_factory=list, max_length=4)


class ConfirmRequest(BaseModel):
    contact_id: int
    content: str = Field(min_length=1, max_length=10000)
    generation_id: int | None = None


class PersonaRequest(BaseModel):
    content: dict[str, Any]
    examples: list[str] = Field(default_factory=list)


class MemoryRequest(BaseModel):
    contact_id: int
    category: str
    content: str = Field(min_length=1, max_length=2000)
    sensitive: bool = False


class RelationshipRequest(BaseModel):
    level: str


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    GUIDANCE_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Persona Chat", version="1.0.0", lifespan=lifespan)
chat_graph = build_graph()
STATIC_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


@app.get("/", include_in_schema=False)
def chat_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


def _save_images(images: list[ImageInput]) -> tuple[list[str], list[str]]:
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    paths: list[str] = []
    data_urls: list[str] = []
    for image in images:
        if image.mime not in allowed:
            raise HTTPException(400, f"不支持的图片类型: {image.mime}")
        encoded = image.data.split(",", 1)[1] if image.data.startswith("data:") and "," in image.data else image.data
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(400, "图片数据不是有效的 Base64") from exc
        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(400, f"单张图片不能超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB")
        filename = f"{uuid4().hex}{allowed[image.mime]}"
        (UPLOAD_DIR / filename).write_bytes(content)
        paths.append(f"/uploads/{filename}")
        data_urls.append(f"data:{image.mime};base64,{base64.b64encode(content).decode('ascii')}")
    return paths, data_urls


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


@app.post("/api/chat/generate")
async def generate(request: GenerateRequest) -> dict[str, Any]:
    content = request.content.strip()
    if not content and not request.images:
        raise HTTPException(400, "请输入消息或选择图片")
    image_paths, image_data_urls = _save_images(request.images)
    saved = store.save_message(
        request.contact_id,
        "user",
        content or "（图片消息）",
        image_paths=image_paths,
    )
    initial_state = {
        "contact_id": request.contact_id,
        "input_message_id": saved["id"],
        "user_request": content or "对方发来了一张图片",
        "image_data_urls": image_data_urls,
        "warnings": [],
    }
    try:
        result = await run_in_threadpool(chat_graph.invoke, initial_state)
    except Exception as exc:
        raise HTTPException(502, f"生成失败，请检查模型配置或服务状态：{exc}") from exc
    return {
        "message": saved,
        "generation_id": result["generation_id"],
        "strategy": result["strategy"],
        "candidates": result["candidates"],
        "relationship": result["relationship"],
        "warnings": result.get("warnings", []),
    }


@app.post("/api/chat/confirm")
def confirm_send(request: ConfirmRequest) -> dict[str, Any]:
    """Record only the candidate the user actually chose or edited."""
    return store.save_message(
        request.contact_id,
        "assistant",
        request.content.strip(),
        source_generation_id=request.generation_id,
    )


@app.post("/api/proactive/{contact_id}")
async def proactive(contact_id: int) -> dict[str, str]:
    provider = store.get_setting("active_model_provider", "default")
    try:
        return await run_in_threadpool(generate_proactive_draft, contact_id, provider)
    except Exception as exc:
        raise HTTPException(502, f"主动聊天草稿生成失败：{exc}") from exc


@app.get("/api/personas/{scope}")
def get_persona(scope: str) -> dict[str, Any]:
    if scope not in {"self", "other"}:
        raise HTTPException(400, "scope 只能是 self 或 other")
    return store.get_persona(scope)


@app.put("/api/personas/{scope}")
def save_persona(scope: str, request: PersonaRequest) -> dict[str, Any]:
    if scope not in {"self", "other"}:
        raise HTTPException(400, "scope 只能是 self 或 other")
    return store.save_persona(scope, request.content, [item.strip() for item in request.examples if item.strip()])


@app.get("/api/memories/{contact_id}")
def list_memories(contact_id: int) -> dict[str, Any]:
    return {"categories": MEMORY_CATEGORIES, "items": store.list_memories(contact_id)}


@app.post("/api/memories")
def add_memory(request: MemoryRequest) -> dict[str, Any]:
    if request.category not in MEMORY_CATEGORIES:
        raise HTTPException(400, "未知记忆分类")
    return store.add_memory(request.contact_id, request.category, request.content, request.sensitive)


@app.put("/api/memories/{memory_id}")
def update_memory(memory_id: int, request: MemoryRequest) -> dict[str, Any]:
    if request.category not in MEMORY_CATEGORIES:
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
def import_chat(request: ImportRequest) -> dict[str, int]:
    records = request.messages or _parse_import_text(request.text)
    count = 0
    for item in records:
        if item.role not in {"user", "assistant"} or not item.content.strip():
            continue
        store.save_message(request.contact_id, item.role, item.content.strip(), created_at=item.created_at)
        count += 1
    return {"imported": count}


@app.get("/api/export")
def export_data() -> JSONResponse:
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


@app.put("/api/models")
def select_model(request: ModelSelectionRequest) -> dict[str, str]:
    if request.provider not in get_model_providers():
        raise HTTPException(400, "模型供应商不存在")
    store.set_setting("active_model_provider", request.provider)
    return {"active": request.provider}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
