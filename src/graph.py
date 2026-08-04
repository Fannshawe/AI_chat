"""LangGraph workflow for persona-consistent companion responses."""

from __future__ import annotations

import json
import operator
import os
from datetime import datetime
from time import perf_counter
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from .agents import get_agent
from .config import SHORT_TERM_MESSAGE_LIMIT
from .guidance import guidance_retriever
from .storage import (
    MEMORY_CATEGORIES,
    MEMORY_OWNER_CONTACT,
    RELATIONSHIP_LEVELS,
    USER_MEMORY_CATEGORIES,
    store,
)


def _env_to_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = _env_to_bool("DEBUG", default=False)


def debug_log(message: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {message}")


def _debug_detail_enabled() -> bool:
    return DEBUG


def _state_debug_enabled(state: "WorkflowState") -> bool:
    value = state.get("debug_enabled")
    if isinstance(value, bool):
        return value
    return DEBUG


def _safe_debug_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_safe_debug_value(item) for item in value[:12]]
    if isinstance(value, tuple):
        return [_safe_debug_value(item) for item in value[:12]]
    if isinstance(value, dict):
        return {str(key): _safe_debug_value(item) for key, item in list(value.items())[:40]}
    return str(value)


def _state_snapshot(state: WorkflowState) -> dict[str, Any]:
    return {
        key: _safe_debug_value(value)
        for key, value in state.items()
        if key != "debug_trace"
    }


def _debug_result_payload(result: dict[str, Any], include_details: bool) -> dict[str, Any] | None:
    if not include_details:
        return None
    sanitized = _safe_debug_value(result)
    if not isinstance(sanitized, dict):
        return sanitized
    return {
        key: value
        for key, value in sanitized.items()
        if key not in {"_debug_llm"}
    }


def _state_delta(before: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    for key, value in output.items():
        if key == "debug_trace":
            continue
        serialized = _safe_debug_value(value)
        if before.get(key) != serialized:
            changed[key] = serialized
    return changed


def _trace_node(name: str, node_fn):
    def wrapped(state: WorkflowState) -> dict[str, Any]:
        started_at = datetime.now().astimezone()
        start_counter = perf_counter()
        include_details = _state_debug_enabled(state)
        before = _state_snapshot(state) if include_details else None
        debug_log(f"node={name} start")
        try:
            result = node_fn(state)
        except Exception as exc:
            finished_at = datetime.now().astimezone()
            entry = {
                "node": name,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_ms": round((perf_counter() - start_counter) * 1000.0, 2),
                "input": before,
                "output": None,
                "state_changes": {} if include_details else None,
                "status": "error",
                "error": str(exc),
                "detail_enabled": include_details,
            }
            debug_log(f"node={name} error={exc}")
            raise
        finished_at = datetime.now().astimezone()
        entry = {
            "node": name,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": round((perf_counter() - start_counter) * 1000.0, 2),
            "input": before,
            "output": _debug_result_payload(result, include_details),
            "llm": _safe_debug_value(result.get("_debug_llm")) if include_details and isinstance(result, dict) else None,
            "state_changes": _state_delta(before or {}, result) if include_details else None,
            "status": "ok",
            "warnings": list(result.get("warnings", [])) if isinstance(result, dict) else [],
            "detail_enabled": include_details,
        }
        debug_log(f"node={name} done duration_ms={entry['duration_ms']}")
        return {**result, "debug_trace": [entry]}

    return wrapped


class WorkflowState(TypedDict, total=False):
    """Shared state for one graph run; parallel branches return partial updates."""

    user_request: str
    contact_id: int
    input_message_id: int | None
    image_data_urls: list[str]
    debug_enabled: bool
    provider_name: str
    history: list[dict[str, Any]]
    persona_core: dict[str, Any]
    persona_personality: dict[str, Any]
    persona_habits: dict[str, Any]
    emotion_profile: dict[str, Any]
    relevant_habits: dict[str, Any]
    relationship: dict[str, Any]
    recent_emotions: list[dict[str, Any]]
    ai_emotion_state: dict[str, Any]
    emotion_analysis: dict[str, Any]
    ai_emotion_analysis: dict[str, Any]
    relationship_analysis: dict[str, Any]
    memory_plan: dict[str, Any]
    memories: list[dict[str, Any]]
    guidance_chunks: list[dict[str, Any]]
    dialogue_composition: dict[str, Any]
    rewrite_instruction: str
    critic_attempts: int
    critic_analysis: dict[str, Any]
    candidates: list[str]
    best_candidates: list[str]
    best_candidate_score: float
    reflection_analysis: dict[str, Any]
    strategy: str
    generation_id: int
    debug_trace: Annotated[list[dict[str, Any]], operator.add]
    warnings: Annotated[list[str], operator.add]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _history_with_time(history: list[dict[str, Any]], limit: int = 24) -> list[dict[str, Any]]:
    return [dict(message) for message in history[-limit:]]


def _emotions_with_time(emotions: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    return [dict(emotion) for emotion in emotions[:limit]]


def _events_with_time(events: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    return [dict(event) for event in events[:limit]]


def _structured_dict(result: dict[str, Any]) -> dict[str, Any]:
    response = result.get("structured_response")
    if isinstance(response, BaseModel):
        return response.model_dump()
    if isinstance(response, dict):
        return response
    raise ValueError("Agent 未返回 structured_response")


def _run_structured_agent(
    state: WorkflowState, agent_name: str, payload: dict[str, Any], fallback: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any] | None]:
    """Invoke one structured agent and return an explicit branch-safe warning list."""
    llm_input = {"messages": [{"role": "user", "content": _json(payload)}]}
    try:
        agent = get_agent(agent_name, state.get("provider_name", "default"))
        result = agent.invoke(llm_input)  # type: ignore[arg-type]
        structured = _structured_dict(result)
        return structured, [], {"input": llm_input, "output": structured}
    except Exception as exc:
        return fallback, [f"{agent_name}节点降级: {exc}"], {"input": llm_input, "output": fallback, "error": str(exc)}


def node_load_context(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: load fixed context. Mutation: state only. Persistence: none."""
    store.initialize()
    contacts = store.list_contacts()
    contact_id = int(state.get("contact_id") or contacts[0]["id"])
    profile = store.get_persona_profile()
    personality = profile.get("personality", {})
    return {
        "contact_id": contact_id,
        "history": store.list_messages(contact_id, SHORT_TERM_MESSAGE_LIMIT),
        "persona_core": profile.get("core", {}),
        "persona_personality": {
            **(personality if isinstance(personality, dict) else {}),
            "expression": profile.get("expression", {}),
        },
        "persona_habits": profile.get("habits", {}),
        "emotion_profile": store.get_emotion_profile(),
        "relationship": store.get_relationship(contact_id),
        "recent_emotions": store.list_emotions(contact_id, 12),
        "ai_emotion_state": store.get_ai_emotion_state(contact_id),
        "provider_name": state.get("provider_name") or store.get_setting("active_model_provider", "default"),
        "critic_attempts": 0,
        "critic_analysis": {"reviews": []},
        "best_candidates": [],
        "best_candidate_score": float("-inf"),
        "rewrite_instruction": "",
    }


def node_emotion_detection(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: detect user emotion only and preserve legacy fields."""
    fallback = {"emotions": [{
        "subject": "对方", "emotion": "unknown", "intensity": 0.0,
        "confidence": 0.2, "reason": "情绪节点未返回可靠结构",
    }]}
    result, warnings, llm_debug = _run_structured_agent(state, "emotion", {
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "recent_emotional_state": _emotions_with_time(state.get("recent_emotions", [])),
        "instruction": "只分析对方当前情绪。证据不足时输出 emotion=unknown。保持 emotion/intensity/confidence/reason 字段。",
    }, fallback)
    emotions = [
        item for item in result.get("emotions", [])
        if isinstance(item, dict) and str(item.get("subject", "对方")) == "对方"
    ]
    if not emotions:
        emotions = fallback["emotions"]
    return {"emotion_analysis": {"emotions": emotions[:4]}, "warnings": warnings, "_debug_llm": llm_debug}


def node_ai_emotion_update(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: update persistent AI emotion state after relationship judgment."""
    previous = state.get("ai_emotion_state", {})
    fallback = {
        "events": [{"event": "其他", "impact": 0.2, "reason": "AI 情绪节点未返回可靠结构"}],
        "ai_emotion": {
            "mood": str(previous.get("mood", "放松")),
            "energy": int(previous.get("energy", 60)),
            "dimensions": [
                {"name": key, "value": int(value)}
                for key, value in (previous.get("dimensions") or {}).items()
            ],
            "reason": "沿用上一轮 AI 情绪状态",
        },
    }
    result, warnings, llm_debug = _run_structured_agent(state, "ai_emotion", {
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "user_emotion": state.get("emotion_analysis", {}),
        "previous_ai_emotion": previous,
        "relationship": state.get("relationship", {}),
        "relationship_judgment": state.get("relationship_analysis", {}),
        "persona_emotion_profile": state.get("emotion_profile", {}),
        "recent_events": _events_with_time(store.list_conversation_events(state["contact_id"], 8)),
    }, fallback)
    return {"ai_emotion_analysis": result, "warnings": warnings, "_debug_llm": llm_debug}


def node_relationship_state(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: propose score events, stage, and boundaries. Persistence: save node."""
    current = state["relationship"]
    fallback = {
        "changes": [], "suggested_level": current["current_level"],
        "stage_reason": "证据不足，保持当前阶段", "serious_event": False,
        "behavior_boundaries": ["保持当前关系阶段对应的自然边界"],
    }
    result, warnings, llm_debug = _run_structured_agent(state, "relationship", {
        "allowed_levels": RELATIONSHIP_LEVELS,
        "current_relationship": current,
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
    }, fallback)
    return {"relationship_analysis": result, "warnings": warnings, "_debug_llm": llm_debug}


def _flatten_habits(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        flattened: list[tuple[str, Any]] = []
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flattened.extend(_flatten_habits(nested, path))
        return flattened
    return [(prefix, value)]


def node_persona_retrieval(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: retrieve topic-relevant Habits only. Persistence: none."""
    query = state["user_request"].lower()
    grams = {query[index:index + 2] for index in range(max(0, len(query) - 1))}
    relevant: dict[str, Any] = {}
    for key, value in _flatten_habits(state.get("persona_habits", {})):
        text = f"{key} {_json(value)}".lower()
        if query and (query in text or any(gram.strip() and gram in text for gram in grams)):
            relevant[key] = value
    return {"relevant_habits": relevant}


def node_memory_planner(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: plan associative retrieval, not retrieve data. Persistence: none."""
    fallback = {"requests": [
        {"memory_type": "semantic", "query": state["user_request"], "keywords": [],
         "priority": 5, "top_k": 5, "time_range": "all", "reason": "检索话题相关事实"},
        {"memory_type": "episodic", "query": state["user_request"], "keywords": [],
         "priority": 3, "top_k": 4, "time_range": "30d", "reason": "检索近期相关经历"},
    ]}
    result, warnings, llm_debug = _run_structured_agent(state, "memory_planner", {
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "emotion_analysis": state.get("emotion_analysis", {}),
        "ai_emotion": state.get("ai_emotion_analysis", {}).get("ai_emotion", state.get("ai_emotion_state", {})),
        "relationship_analysis": state.get("relationship_analysis", {}),
        "available_memory_types": USER_MEMORY_CATEGORIES,
        "instruction": "扩展话题关键词和近义联想，但不要臆造事实。",
    }, fallback)
    return {"memory_plan": result, "warnings": warnings, "_debug_llm": llm_debug}


def node_memory_retrieval(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: execute Memory Plan against categorized storage. Persistence: none."""
    requests = state.get("memory_plan", {}).get("requests", [])
    contact_memories = store.search_memories_by_plan(state["contact_id"], requests)
    ai_profile = store.get_ai_memory_profile()
    ai_memories: list[dict[str, Any]] = []
    for category in MEMORY_CATEGORIES:
        for index, item in enumerate(ai_profile.get(category, []) or []):
            if isinstance(item, dict):
                content = str(item.get("content", "")).strip()
                if not content:
                    continue
                ai_memories.append({
                    "id": f"ai:{category}:{index}",
                    "category": category,
                    "content": content,
                    "source": str(item.get("source", "admin")),
                    "source_label": f"AI memory / {item.get('source', 'admin')}",
                    "owner_type": "ai",
                    "owner_label": "ai",
                    "retrieval_priority": 6,
                })
            else:
                content = str(item).strip()
                if not content:
                    continue
                ai_memories.append({
                    "id": f"ai:{category}:{index}",
                    "category": category,
                    "content": content,
                    "source": "admin",
                    "source_label": "AI memory / admin",
                    "owner_type": "ai",
                    "owner_label": "ai",
                    "retrieval_priority": 6,
                })
    labeled_contact = [
        {
            **item,
            "source": "reflection",
            "source_label": "contact memory / reflection",
            "owner_type": item.get("owner_type", MEMORY_OWNER_CONTACT),
            "owner_label": item.get("owner_label", "contact"),
        }
        for item in contact_memories
    ]
    return {"memories": [*ai_memories, *labeled_contact]}


def node_chat_guidance(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: retrieve relevant chat-skill documents. Persistence: none."""
    query = " ".join([
        state["user_request"], state["relationship"]["current_level"],
        _json(state.get("emotion_analysis", {})),
    ])
    return {"guidance_chunks": guidance_retriever.search(query, limit=5)}


def node_dialogue_composer(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: produce an eight-part response plan, never final text. Persistence: snapshot."""
    fallback = {
        "goal": "回应当前消息并自然延续对话",
        "strategy": ["先回应对方当前表达", "没有必要时不追问"],
        "structure": ["简短直接回应"],
        "memory_usage": ["只使用明确相关且已存储的记忆"],
        "persona_expression": ["遵守固定人格与表达风格"],
        "emotion_expression": ["不过度解读情绪"],
        "topic_plan": ["优先延续当前话题"],
        "constraints": ["不捏造事实", "不越过关系行为边界"],
    }
    result, warnings, llm_debug = _run_structured_agent(state, "dialogue_composer", {
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "persona": {"core": state.get("persona_core", {}), "personality": state.get("persona_personality", {})},
        "relevant_habits": state.get("relevant_habits", {}),
        "emotion": state.get("emotion_analysis", {}),
        "ai_emotion": state.get("ai_emotion_analysis", {}).get("ai_emotion", state.get("ai_emotion_state", {})),
        "relationship": state.get("relationship", {}),
        "relationship_judgment": state.get("relationship_analysis", {}),
        "memories": state.get("memories", []),
        "chat_skill_documents": state.get("guidance_chunks", []),
    }, fallback)
    return {
        "dialogue_composition": result,
        "strategy": str(result.get("goal", "")),
        "warnings": warnings,
        "_debug_llm": llm_debug,
    }


def _prompt_composer_payload(state: WorkflowState) -> dict[str, Any]:
    return {
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "persona": {"core": state.get("persona_core", {}), "personality": state.get("persona_personality", {})},
        "relevant_habits": state.get("relevant_habits", {}),
        "emotion_analysis": state.get("emotion_analysis", {}),
        "ai_emotion": state.get("ai_emotion_analysis", {}).get("ai_emotion", state.get("ai_emotion_state", {})),
        "relationship": state.get("relationship", {}),
        "relationship_boundaries": state.get("relationship_analysis", {}).get("behavior_boundaries", []),
        "relevant_memories": state.get("memories", []),
        "dialogue_plan": state.get("dialogue_composition", {}),
        "rewrite_instruction": state.get("rewrite_instruction", ""),
        "candidate_count": 3,
    }


def _invoke_prompt_composer(state: WorkflowState, include_images: bool) -> tuple[list[str], dict[str, Any] | None]:
    agent = get_agent("prompt_composer", state.get("provider_name", "default"))
    payload = _prompt_composer_payload(state)
    images = (state.get("image_data_urls") or []) if include_images else []
    content: str | list[dict[str, Any]] = _json(payload)
    if images:
        content = [{"type": "text", "text": _json(payload)}]
        content.extend({"type": "image_url", "image_url": {"url": image}} for image in images)
    llm_input = {"messages": [{"role": "user", "content": content}]}
    try:
        result = agent.invoke(llm_input)  # type: ignore[arg-type]
    except Exception:
        if not images:
            raise
        llm_input = {"messages": [{"role": "user", "content": _json(payload)}]}
        result = agent.invoke(llm_input)  # type: ignore[arg-type]
    structured = _structured_dict(result)
    candidates = [str(item).strip() for item in structured["candidates"]]
    return candidates, {"input": llm_input, "output": structured}


def node_prompt_composer(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: write final candidate replies from the plan. Persistence: snapshot only."""
    try:
        candidates, llm_debug = _invoke_prompt_composer(state, include_images=state.get("critic_attempts", 0) == 0)
        return {"candidates": candidates, "_debug_llm": llm_debug}
    except Exception as exc:
        if state.get("best_candidates"):
            return {"candidates": state["best_candidates"], "warnings": [f"Prompt Composer重写失败: {exc}"]}
        raise


def _critic_score(review: dict[str, Any]) -> float:
    critiques = review.get("candidates", [])
    if not critiques:
        return float("-inf")
    return sum(
        float(item.get("naturalness", 0)) - float(item.get("ai_taste", 10))
        - float(item.get("interview_taste", 10)) - float(item.get("empathy_excess", 10))
        for item in critiques
    ) / len(critiques)


def node_reply_critic(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: review Style, Persona, and Memory use. Persistence: snapshot only."""
    fallback = {"candidates": [], "passed": True, "rewrite_instruction": ""}
    review, warnings, llm_debug = _run_structured_agent(state, "critic", {
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "persona": {"core": state.get("persona_core", {}), "personality": state.get("persona_personality", {})},
        "relevant_memories": state.get("memories", []),
        "dialogue_plan": state.get("dialogue_composition", {}),
        "candidates": state["candidates"],
        "review_dimensions": ["style", "persona", "memory"],
    }, fallback)
    attempts = state.get("critic_attempts", 0) + 1
    history = [*state.get("critic_analysis", {}).get("reviews", []), review]
    score = _critic_score(review)
    best_score = state.get("best_candidate_score", float("-inf"))
    best_candidates = state.get("best_candidates", [])
    if score > best_score or not best_candidates:
        best_score = score
        best_candidates = list(state["candidates"])
    result: dict[str, Any] = {
        "critic_attempts": attempts,
        "critic_analysis": {"reviews": history, "passed": bool(review.get("passed", True))},
        "rewrite_instruction": str(review.get("rewrite_instruction", "")),
        "best_candidate_score": best_score,
        "best_candidates": best_candidates,
        "warnings": warnings,
        "_debug_llm": llm_debug,
    }
    if not review.get("passed", True) and attempts >= 3:
        result["warnings"] = [*warnings, "Reply Critic两次重写后仍未通过，已返回最佳候选并记录告警。"]
    return result


def route_after_critic(state: WorkflowState) -> str:
    if state.get("critic_analysis", {}).get("passed", True) or state.get("critic_attempts", 0) >= 3:
        return "reflection"
    return "prompt_composer"


def node_reflection(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: decide durable memories and Persona updates. Persistence: save node."""
    fallback = {"memories": [], "habit_updates": [], "core_update_proposals": []}
    result, warnings, llm_debug = _run_structured_agent(state, "reflection", {
        "latest_real_message": {"speaker": "对方", "content": state["user_request"]},
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "generated_candidates": state.get("candidates", []),
        "existing_relevant_memories": state.get("memories", []),
        "ai_emotion": state.get("ai_emotion_analysis", {}).get("ai_emotion", state.get("ai_emotion_state", {})),
        "current_habits": state.get("persona_habits", {}),
        "allowed_memory_types": USER_MEMORY_CATEGORIES,
        "rules": {"memory": "只保存值得长期记住且有明确证据的内容", "habits": "可自动更新",
                  "core": "只能生成待人工确认的提案", "personality": "禁止修改"},
    }, fallback)
    return {"reflection_analysis": result, "warnings": warnings, "_debug_llm": llm_debug}


def node_content_save(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: persist selected analyses. Mutation and persistence: yes."""
    final_candidates = state.get("candidates", [])
    if not state.get("critic_analysis", {}).get("passed", True) and state.get("critic_attempts", 0) >= 3:
        final_candidates = state.get("best_candidates", final_candidates)

    input_message_id = state.get("input_message_id")
    if not input_message_id:
        saved = store.save_message(state["contact_id"], "user", state["user_request"])
        input_message_id = int(saved["id"])
    for item in state.get("emotion_analysis", {}).get("emotions", [])[:4]:
        if isinstance(item, dict):
            store.add_emotion(state["contact_id"], item)
    relationship = store.apply_relationship_analysis(state["contact_id"], state.get("relationship_analysis", {}))
    ai_emotion_state = store.save_ai_emotion_state(
        state["contact_id"], state.get("ai_emotion_analysis", {}).get("ai_emotion", {})
    )
    store.add_conversation_events(state["contact_id"], state.get("ai_emotion_analysis", {}).get("events", []))
    reflection = state.get("reflection_analysis", {})
    for item in reflection.get("memories", [])[:8]:
        if not isinstance(item, dict) or not str(item.get("content", "")).strip():
            continue
        category = str(item.get("category", "semantic"))
        if category not in USER_MEMORY_CATEGORIES:
            category = "semantic"
        store.add_memory(state["contact_id"], category, str(item["content"]),
                         bool(item.get("sensitive", False)), input_message_id,
                         owner_type=MEMORY_OWNER_CONTACT)
    store.update_persona_habits(reflection.get("habit_updates", []))
    store.add_core_update_proposals(state["contact_id"], reflection.get("core_update_proposals", []))
    analysis = {
        "emotion": state.get("emotion_analysis", {}), "relationship": state.get("relationship_analysis", {}),
        "ai_emotion": state.get("ai_emotion_analysis", {}),
        "memory_plan": state.get("memory_plan", {}), "retrieved_memories": state.get("memories", []),
        "dialogue_composer": state.get("dialogue_composition", {}), "critic": state.get("critic_analysis", {}),
        "reflection": reflection, "warnings": state.get("warnings", []),
        "debug_trace": state.get("debug_trace", []),
    }
    generation_id = store.save_generation(
        state["contact_id"], input_message_id, state.get("strategy", ""), final_candidates, analysis,
    )
    return {
        "input_message_id": input_message_id,
        "relationship": relationship,
        "generation_id": generation_id,
        "candidates": final_candidates,
    }


def build_graph():
    graph = StateGraph(WorkflowState)
    graph.add_node("context", _trace_node("context", node_load_context))
    graph.add_node("emotion", _trace_node("emotion", node_emotion_detection))
    graph.add_node("relationship", _trace_node("relationship", node_relationship_state))
    graph.add_node("persona_retrieval", _trace_node("persona_retrieval", node_persona_retrieval))
    graph.add_node("ai_emotion", _trace_node("ai_emotion", node_ai_emotion_update))
    graph.add_node("memory_planner", _trace_node("memory_planner", node_memory_planner))
    graph.add_node("memory_retrieval", _trace_node("memory_retrieval", node_memory_retrieval))
    graph.add_node("guidance", _trace_node("guidance", node_chat_guidance))
    graph.add_node("dialogue_composer", _trace_node("dialogue_composer", node_dialogue_composer))
    graph.add_node("prompt_composer", _trace_node("prompt_composer", node_prompt_composer))
    graph.add_node("critic", _trace_node("critic", node_reply_critic))
    graph.add_node("reflection", _trace_node("reflection", node_reflection))
    graph.add_node("save", _trace_node("save", node_content_save))
    graph.set_entry_point("context")
    graph.add_edge("context", "emotion")
    graph.add_edge("context", "relationship")
    graph.add_edge("context", "persona_retrieval")
    graph.add_edge("emotion", "ai_emotion")
    graph.add_edge("relationship", "ai_emotion")
    graph.add_edge("persona_retrieval", "ai_emotion")
    graph.add_edge("ai_emotion", "memory_planner")
    graph.add_edge("memory_planner", "memory_retrieval")
    graph.add_edge("memory_retrieval", "guidance")
    graph.add_edge("guidance", "dialogue_composer")
    graph.add_edge("dialogue_composer", "prompt_composer")
    graph.add_edge("prompt_composer", "critic")
    graph.add_conditional_edges("critic", route_after_critic, {
        "prompt_composer": "prompt_composer", "reflection": "reflection",
    })
    graph.add_edge("reflection", "save")
    graph.add_edge("save", END)
    return graph.compile()


def generate_proactive_draft(contact_id: int, provider_name: str = "default") -> dict[str, str]:
    store.initialize()
    payload = {
        "recent_conversation_with_time": _history_with_time(store.list_messages(contact_id, 30), 30),
        "relationship": store.get_relationship(contact_id),
        "persona": store.get_persona_profile(),
        "memories": store.list_memories(contact_id, 80, owner_type=MEMORY_OWNER_CONTACT),
    }
    result = get_agent("proactive", provider_name).invoke({
        "messages": [{"role": "user", "content": _json(payload)}]
    })
    parsed = _structured_dict(result)
    return {"draft": str(parsed["draft"]), "reason": str(parsed["reason"])}