"""LangGraph workflow for persona-consistent relationship chat suggestions.

The public ``build_graph()`` entry point is retained from the original project.
Model clients are created lazily, so importing this module does not require a
working API endpoint.
"""

from __future__ import annotations

import json
import os
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from .agents import get_agent
from .config import SHORT_TERM_MESSAGE_LIMIT
from .guidance import guidance_retriever
from .storage import MEMORY_CATEGORIES, RELATIONSHIP_LEVELS, store

def _env_to_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = _env_to_bool("DEBUG", default=False)


def debug_log(message: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {message}")


class WorkflowState(TypedDict):
    """Shared state for one graph run."""

    user_request: str
    contact_id: int
    input_message_id: int | None
    image_data_urls: list[str]
    provider_name: str
    history: list[dict[str, Any]]
    self_persona: dict[str, Any]
    other_persona: dict[str, Any]
    relationship: dict[str, Any]
    recent_emotions: list[dict[str, Any]]
    life_state: dict[str, Any] | None
    life_state_history: list[dict[str, Any]]
    emotion_analysis: dict[str, Any]
    relationship_analysis: dict[str, Any]
    memories: list[dict[str, Any]]
    persona_analysis: dict[str, Any]
    guidance_chunks: list[dict[str, Any]]
    guidance_analysis: dict[str, Any]
    dialogue_policy: dict[str, Any]
    critic_analysis: dict[str, Any]
    memory_analysis: dict[str, Any]
    strategy: str
    candidates: list[str]
    generation_id: int
    warnings: list[str]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _history_text(history: list[dict[str, Any]], limit: int = 24) -> str:
    labels = {"user": "对方", "assistant": "我", "system": "系统"}
    lines = []
    for message in history[-limit:]:
        label = labels.get(str(message.get("role")), str(message.get("role", "未知")))
        content = str(message.get("content", ""))
        images = message.get("image_paths") or []
        suffix = f" [附带{len(images)}张图片]" if images else ""
        lines.append(f"{label}: {content}{suffix}")
    return "\n".join(lines)


def _structured_dict(result: dict[str, Any]) -> dict[str, Any]:
    """Convert LangChain's validated ``structured_response`` to graph state."""
    response = result.get("structured_response")
    if isinstance(response, BaseModel):
        return response.model_dump()
    if isinstance(response, dict):
        return response
    raise ValueError("Agent 未返回 structured_response")


def _run_structured_agent(
    state: WorkflowState,
    agent_name: str,
    payload: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Invoke a named agent and degrade to ``fallback`` (with a warning) on failure."""
    try:
        agent = get_agent(agent_name, state.get("provider_name", "default"))
        result = agent.invoke({"messages": [{"role": "user", "content": _json(payload)}]})
        return _structured_dict(result)
    except Exception as exc:
        state.setdefault("warnings", []).append(f"{agent_name}节点降级: {exc}")
        return fallback


def node_load_context(state: WorkflowState) -> WorkflowState:
    """Load short-term state while leaving the incoming request untouched."""
    store.initialize()
    contacts = store.list_contacts()
    contact_id = int(state.get("contact_id") or contacts[0]["id"])
    state["contact_id"] = contact_id
    state["history"] = store.list_messages(contact_id, SHORT_TERM_MESSAGE_LIMIT)
    state["self_persona"] = store.get_persona("self")
    state["other_persona"] = store.get_persona("other")
    state["relationship"] = store.get_relationship(contact_id)
    state["recent_emotions"] = store.list_emotions(contact_id, 12)
    state["life_state"] = store.get_life_state()
    state["life_state_history"] = store.list_life_states(7)
    state["provider_name"] = state.get("provider_name") or store.get_setting("active_model_provider", "default")
    state.setdefault("warnings", [])
    return state


def node_emotion_detection(state: WorkflowState) -> WorkflowState:
    fallback = {"emotions": [{
        "subject": "对方", "emotion": "平静", "intensity": 0.5,
        "confidence": 0.2, "reason": "情绪节点未返回可靠结构",
    }]}
    payload = {
        "latest_message": state["user_request"],
        "recent_conversation": _history_text(state.get("history", [])),
        "recent_emotional_state": state.get("recent_emotions", []),
    }
    state["emotion_analysis"] = _run_structured_agent(state, "emotion", payload, fallback)
    return state


def node_relationship_state(state: WorkflowState) -> WorkflowState:
    current = state["relationship"]["current_level"]
    fallback = {"suggested_level": current, "confidence": 0.0, "reason": "证据不足，保持当前阶段"}
    payload = {
        "allowed_levels": RELATIONSHIP_LEVELS,
        "current_level": current,
        "latest_message": state["user_request"],
        "recent_conversation": _history_text(state.get("history", [])),
        "emotion_analysis": state["emotion_analysis"],
    }
    state["relationship_analysis"] = _run_structured_agent(state, "relationship", payload, fallback)
    return state


def node_memory_retrieval(state: WorkflowState) -> WorkflowState:
    """Retrieve long-term memory with local keyword matching."""
    query = " ".join([
        state["user_request"],
        str(state.get("relationship_analysis", {}).get("reason", "")),
        str(state.get("emotion_analysis", {})),
    ])
    state["memories"] = store.search_memories(state["contact_id"], query, limit=12)
    return state


def node_persona_engine(state: WorkflowState) -> WorkflowState:
    fallback = {
        "style_instruction": "保持自然、简短，严格使用手工人格档案与示例中的表达习惯。",
        "counterpart_observations": [],
        "conflicts": [],
    }
    payload = {
        "self_persona": state["self_persona"],
        "counterpart_persona": state["other_persona"],
        "relevant_memories": state.get("memories", []),
        "latest_message": state["user_request"],
        "recent_conversation": _history_text(state.get("history", [])),
        "manually_authored_life_state": state.get("life_state"),
        "recent_life_state_history": state.get("life_state_history", []),
    }
    state["persona_analysis"] = _run_structured_agent(state, "persona", payload, fallback)
    return state


def node_chat_guidance(state: WorkflowState) -> WorkflowState:
    query = f"{state['user_request']} {state['relationship']['current_level']} {_json(state['emotion_analysis'])}"
    state["guidance_chunks"] = guidance_retriever.search(query, limit=5)
    fallback = {
        "strategy": "先回应对方当前表达，再自然推进话题；没有足够信息时不要过度解读。",
        "tone": "自然、克制",
        "avoid": ["捏造事实", "关系推进过快"],
        "document_basis": [],
    }
    payload = {
        "latest_message": state["user_request"],
        "recent_conversation": _history_text(state.get("history", [])),
        "relationship": state["relationship"],
        "relationship_suggestion": state["relationship_analysis"],
        "emotions": state["emotion_analysis"],
        "retrieved_guidance": state["guidance_chunks"],
    }
    state["guidance_analysis"] = _run_structured_agent(state, "guidance", payload, fallback)
    state["strategy"] = str(state["guidance_analysis"].get("strategy", fallback["strategy"]))
    return state


def _recent_question_density(history: list[dict[str, Any]], limit: int = 6) -> dict[str, int]:
    own_messages = [item for item in history if item.get("role") == "assistant"][-limit:]
    question_messages = sum(
        1 for item in own_messages if "?" in str(item.get("content", "")) or "？" in str(item.get("content", ""))
    )
    return {"own_message_count": len(own_messages), "question_message_count": question_messages}


def node_dialogue_policy(state: WorkflowState) -> WorkflowState:
    fallback = {
        "primary_mode": "简短回应",
        "candidate_modes": ["简短回应", "分享自己", "开玩笑/调侃"],
        "question_probability": 0.15,
        "question_budget": 0,
        "should_resist": False,
        "rationale": "策略节点不可用，默认降低追问并保持自然简短",
    }
    payload = {
        "latest_message": state["user_request"],
        "recent_conversation": _history_text(state.get("history", [])),
        "recent_question_density": _recent_question_density(state.get("history", [])),
        "current_relationship": state["relationship"],
        "emotions": state["emotion_analysis"],
        "chat_guidance": state["guidance_analysis"],
        "retrieved_guidance": state["guidance_chunks"],
        "manually_authored_life_state": state.get("life_state"),
    }
    state["dialogue_policy"] = _run_structured_agent(state, "dialogue_policy", payload, fallback)
    return state


def _response_payload(state: WorkflowState, rewrite_instruction: str = "") -> dict[str, Any]:
    return {
        "latest_message": state["user_request"],
        "recent_conversation": _history_text(state.get("history", [])),
        "current_relationship": state["relationship"]["current_level"],
        "emotion_analysis": state["emotion_analysis"],
        "relevant_memories": state.get("memories", []),
        "persona_instruction": state["persona_analysis"],
        "manually_authored_life_state": state.get("life_state"),
        "chat_guidance": state["guidance_analysis"],
        "dialogue_policy": state["dialogue_policy"],
        "rewrite_instruction": rewrite_instruction,
        "candidate_count": 3,
    }


def _invoke_response_agent(state: WorkflowState, payload: dict[str, Any], include_images: bool) -> list[str]:
    response_agent = get_agent("response", state.get("provider_name", "default"))
    images = state.get("image_data_urls") or [] if include_images else []
    message_content: str | list[dict[str, Any]] = _json(payload)
    if images:
        message_content = [{"type": "text", "text": _json(payload)}]
        message_content.extend({"type": "image_url", "image_url": {"url": image}} for image in images)
    try:
        result = response_agent.invoke({"messages": [{"role": "user", "content": message_content}]})
    except Exception as exc:
        if not images:
            raise
        state.setdefault("warnings", []).append(f"当前模型无法处理图片，已按文字重试: {exc}")
        result = response_agent.invoke({"messages": [{"role": "user", "content": _json(payload)}]})
    return [str(item).strip() for item in _structured_dict(result)["candidates"]]


def node_response_output(state: WorkflowState) -> WorkflowState:
    state["candidates"] = _invoke_response_agent(state, _response_payload(state), include_images=True)
    return state


def node_reply_critic(state: WorkflowState) -> WorkflowState:
    fallback = {"candidates": [], "passed": True, "rewrite_instruction": ""}
    payload = {
        "latest_message": state["user_request"],
        "recent_conversation": _history_text(state.get("history", [])),
        "dialogue_policy": state["dialogue_policy"],
        "persona_instruction": state["persona_analysis"],
        "candidates": state["candidates"],
    }
    first_review = _run_structured_agent(state, "critic", payload, fallback)
    state["critic_analysis"] = {"first_review": first_review, "regenerated": False}
    if first_review.get("passed", True):
        return state

    rewrite = str(first_review.get("rewrite_instruction", "降低AI味、追问和空洞共情，改得更短更口语。"))
    try:
        state["candidates"] = _invoke_response_agent(
            state, _response_payload(state, rewrite_instruction=rewrite), include_images=False,
        )
        second_payload = {**payload, "candidates": state["candidates"], "previous_issues": first_review}
        second_review = _run_structured_agent(state, "critic", second_payload, fallback)
        state["critic_analysis"] = {
            "first_review": first_review, "regenerated": True, "final_review": second_review,
        }
        if not second_review.get("passed", True):
            state.setdefault("warnings", []).append("候选重写后仍有真人感质量预警，请发送前人工检查。")
    except Exception as exc:
        state.setdefault("warnings", []).append(f"候选自动重写失败，保留初稿: {exc}")
    return state


def node_content_save(state: WorkflowState) -> WorkflowState:
    """Persist analyses and memory; candidates remain drafts until confirmed."""
    input_message_id = state.get("input_message_id")
    if not input_message_id:
        saved = store.save_message(state["contact_id"], "user", state["user_request"])
        input_message_id = int(saved["id"])
        state["input_message_id"] = input_message_id

    emotions = state.get("emotion_analysis", {}).get("emotions", [])
    if isinstance(emotions, list):
        for item in emotions[:4]:
            if isinstance(item, dict):
                store.add_emotion(state["contact_id"], item)

    relationship = state.get("relationship_analysis", {})
    confidence = float(relationship.get("confidence", 0) or 0)
    state["relationship"] = store.apply_relationship_analysis(
        state["contact_id"], relationship.get("suggested_level"), confidence,
        str(relationship.get("reason", "")),
    )

    memory_fallback = {"memories": []}
    payload = {
        "latest_real_message": {"speaker": "对方", "content": state["user_request"]},
        "existing_relevant_memories": state.get("memories", []),
        "allowed_categories": MEMORY_CATEGORIES,
    }
    state["memory_analysis"] = _run_structured_agent(state, "memory", payload, memory_fallback)
    extracted = state["memory_analysis"].get("memories", []) if isinstance(state["memory_analysis"], dict) else []
    for item in extracted[:8] if isinstance(extracted, list) else []:
        if not isinstance(item, dict) or not str(item.get("content", "")).strip():
            continue
        category = str(item.get("category", "人物事实与偏好"))
        if category not in MEMORY_CATEGORIES:
            category = "人物事实与偏好"
        store.add_memory(
            state["contact_id"], category, str(item["content"]),
            bool(item.get("sensitive", False)), input_message_id,
        )

    analysis = {
        "emotion": state.get("emotion_analysis", {}),
        "relationship": state.get("relationship_analysis", {}),
        "persona": state.get("persona_analysis", {}),
        "guidance": state.get("guidance_analysis", {}),
        "dialogue_policy": state.get("dialogue_policy", {}),
        "critic": state.get("critic_analysis", {}),
        "life_state": state.get("life_state"),
        "memory": state.get("memory_analysis", {}),
        "warnings": state.get("warnings", []),
    }
    state["generation_id"] = store.save_generation(
        state["contact_id"], input_message_id, state["strategy"], state["candidates"], analysis,
    )
    return state


def build_graph():
    graph = StateGraph(WorkflowState)
    graph.add_node("context", node_load_context)
    graph.add_node("emotion", node_emotion_detection)
    graph.add_node("relationship", node_relationship_state)
    graph.add_node("memory", node_memory_retrieval)
    graph.add_node("persona", node_persona_engine)
    graph.add_node("guidance", node_chat_guidance)
    graph.add_node("dialogue_policy", node_dialogue_policy)
    graph.add_node("output", node_response_output)
    graph.add_node("critic", node_reply_critic)
    graph.add_node("save", node_content_save)
    graph.set_entry_point("context")
    graph.add_edge("context", "emotion")
    graph.add_edge("emotion", "relationship")
    graph.add_edge("relationship", "memory")
    graph.add_edge("memory", "persona")
    graph.add_edge("persona", "guidance")
    graph.add_edge("guidance", "dialogue_policy")
    graph.add_edge("dialogue_policy", "output")
    graph.add_edge("output", "critic")
    graph.add_edge("critic", "save")
    graph.add_edge("save", END)
    return graph.compile()


def generate_proactive_draft(contact_id: int, provider_name: str = "default") -> dict[str, str]:
    store.initialize()
    history = store.list_messages(contact_id, 30)
    payload = {
        "recent_conversation": _history_text(history),
        "relationship": store.get_relationship(contact_id),
        "persona": store.get_persona("self"),
        "manually_authored_life_state": store.get_life_state(),
        "memories": store.list_memories(contact_id, 80),
    }
    proactive_agent = get_agent("proactive", provider_name)
    result = proactive_agent.invoke({"messages": [{"role": "user", "content": _json(payload)}]})
    parsed = _structured_dict(result)
    return {"draft": str(parsed["draft"]), "reason": str(parsed["reason"])}
