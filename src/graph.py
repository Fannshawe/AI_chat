"""LangGraph workflow for persona-consistent companion responses."""

from __future__ import annotations

import json
import operator
import os
import re
import sys
from datetime import datetime
from time import perf_counter
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from .agents import get_agent
from .config import CRITIC_MAX_ATTEMPTS, SHORT_TERM_MESSAGE_LIMIT, WORKFLOW_DEBUG
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

_PERSONA_QUERY_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "电影": ("电影", "影片", "片子", "favorite_movies"),
    "音乐": ("音乐", "歌", "听歌", "music"),
    "作息": ("作息", "睡觉", "几点睡", "几点起", "sleep_time", "wake_up"),
    "工作": ("工作", "上班", "work_style"),
    "喜欢": ("喜欢", "喜好", "爱好", "likes", "topics"),
    "不喜欢": ("不喜欢", "讨厌", "雷点", "dislikes"),
    "最近": ("最近", "近况", "recent_focus"),
}


def debug_log(message: str) -> None:
    if DEBUG:
        _safe_debug_print(f"[DEBUG] {message}")


def _safe_debug_print(text: str) -> None:
    """Print debug text without crashing on non-UTF8 Windows console encodings."""
    try:
        print(text)
        return
    except UnicodeEncodeError:
        pass

    stream = sys.stdout
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8", errors="backslashreplace") + b"\n")
        stream.flush()
        return
    stream.write(text.encode("utf-8", errors="backslashreplace").decode("utf-8") + "\n")
    stream.flush()


def _debug_detail_enabled() -> bool:
    return DEBUG


def _state_debug_enabled(state: "WorkflowState") -> bool:
    value = state.get("debug_enabled")
    if isinstance(value, bool):
        return value
    return DEBUG


def _workflow_debug_enabled(state: "WorkflowState") -> bool:
    return _state_debug_enabled(state) or WORKFLOW_DEBUG


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


def _node_output_payload(name: str, result: dict[str, Any], include_details: bool) -> dict[str, Any] | None:
    """Return sanitized node output for nodes where output visibility is useful."""
    _OUTPUT_NODES = {"memory_retrieval", "persona_habit_retrieval"}
    if not include_details or name not in _OUTPUT_NODES:
        return None
    sanitized = _safe_debug_value(result)
    if not isinstance(sanitized, dict):
        return sanitized
    return {key: value for key, value in sanitized.items() if key not in {"_debug_llm"}}


def _state_delta(before: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    for key, value in output.items():
        if key == "debug_trace":
            continue
        serialized = _safe_debug_value(value)
        if before.get(key) != serialized:
            changed[key] = serialized
    return changed


def _workflow_debug_print(state: "WorkflowState", label: str, payload: Any) -> None:
    if not _workflow_debug_enabled(state):
        return
    formatted = json.dumps(_safe_debug_value(payload), ensure_ascii=False, indent=2)
    _safe_debug_print(f"[workflow-debug] {label}\n{formatted}\n[/workflow-debug]")


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
            "output": _node_output_payload(name, result, include_details) if isinstance(result, dict) else None,
            "llm": _safe_debug_value(result.get("_debug_llm")) if include_details and isinstance(result, dict) else None,
            "state_changes": _state_delta(before or {}, result) if include_details else None,
            "status": "ok",
            "warnings": list(result.get("warnings", [])) if isinstance(result, dict) else [],
            "detail_enabled": include_details,
        }
        debug_log(f"node={name} done duration_ms={entry['duration_ms']}")
        return {**result, "debug_trace": [entry]}

    return wrapped


class _WorkflowStateRequired(TypedDict):
    """Keys that must exist for the graph nodes that use direct indexing."""

    user_request: str
    contact_id: int
    relationship: dict[str, Any]
    candidates: list[str]


class WorkflowState(_WorkflowStateRequired, total=False):
    """Shared state for one graph run; parallel branches return partial updates."""

    input_message_id: int | None
    debug_enabled: bool
    provider_name: str
    history: list[dict[str, Any]]
    persona_core: dict[str, Any]
    persona_personality: dict[str, Any]
    persona_habits: dict[str, Any]
    emotion_profile: dict[str, Any]
    relevant_habits: dict[str, Any]
    recent_emotions: list[dict[str, Any]]
    self_emotion_state: dict[str, Any]
    contact_emotion_analysis: dict[str, Any]
    self_emotion_update: dict[str, Any]
    relationship_analysis: dict[str, Any]
    memory_plan: dict[str, Any]
    memories: list[dict[str, Any]]
    primary_memories: list[dict[str, Any]]
    background_memories: list[dict[str, Any]]
    guidance_chunks: list[dict[str, Any]]
    response_composition: dict[str, Any]
    critic_attempts: int
    critic_analysis: dict[str, Any]
    user_memory_recognition: dict[str, Any]
    strategy: str
    generation_id: int
    debug_trace: Annotated[list[dict[str, Any]], operator.add]
    warnings: Annotated[list[str], operator.add]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


_STRUCTURED_LINE_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+[.、)]|```|\|.+\|)")
_MAX_MESSAGE_SEGMENTS = 5


def _looks_structured(line: str) -> bool:
    """Detect list/code/table lines that must stay in one bubble, not split apart."""
    return bool(_STRUCTURED_LINE_PATTERN.match(line))


def _normalize_segments(text: str, max_segments: int = _MAX_MESSAGE_SEGMENTS) -> str:
    """Collapse model line breaks into clean, bubble-ready WeChat-style segments.

    The model is asked to separate distinct ideas with a single newline. This
    guards against blank-line padding, over-splitting, and accidentally
    splitting structured content (lists/code/tables) across bubbles.
    """
    stripped = text.strip()
    if stripped == "（暂不回复）":
        return stripped
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if not lines:
        return stripped
    if any(_looks_structured(line) for line in lines):
        return "\n".join(lines)
    if len(lines) > max_segments:
        lines = [*lines[: max_segments - 1], "".join(lines[max_segments - 1:])]
    return "\n".join(lines)


def _history_with_time(history: list[dict[str, Any]], limit: int = 24) -> list[dict[str, Any]]:
    return [dict(message) for message in history[-limit:]]


def _emotions_with_time(emotions: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    return [dict(emotion) for emotion in emotions[:limit]]


def _events_with_time(events: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    return [dict(event) for event in events[:limit]]


def _primary_contact_emotion_summary(state: WorkflowState) -> dict[str, Any]:
    emotions = [
        item for item in state.get("contact_emotion_analysis", {}).get("emotions", [])
        if isinstance(item, dict) and str(item.get("subject", "对方")) == "对方"
    ]
    primary = emotions[0] if emotions else {}
    return {
        "emotion": primary.get("emotion", "unknown"),
        "intensity": primary.get("intensity", 0.0),
        "confidence": primary.get("confidence", 0.0),
        "reason": primary.get("reason", ""),
    }


def _self_emotion_state_summary(state: WorkflowState) -> dict[str, Any]:
    self_emotion = state.get("self_emotion_update", {}).get("self_emotion_state", state.get("self_emotion_state", {}))
    dimensions = [item for item in self_emotion.get("dimensions", []) if isinstance(item, dict)]
    return {
        "mood": self_emotion.get("mood"),
        "mood_intensity": self_emotion.get("mood_intensity"),
        "energy": self_emotion.get("energy"),
        "dimensions": dimensions[:2],
        "reason": self_emotion.get("reason", ""),
    }


def _reflection_memories_summary(memories: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    for item in memories[:limit]:
        if not isinstance(item, dict):
            continue
        summarized.append({
            "category": item.get("category"),
            "content": item.get("content"),
        })
    return summarized


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
        "self_emotion_state": store.get_self_emotion_state(contact_id),
        "provider_name": state.get("provider_name") or store.get_setting("active_model_provider", "default"),
        "critic_attempts": 0,
        "critic_analysis": {"reviews": []},
    }


def node_turn_interpreter(state: WorkflowState) -> dict[str, Any]:
    """Interpret user emotion and relationship evidence in one shared-context call."""
    emotion_fallback = {"emotions": [{
        "subject": "对方", "emotion": "unknown", "intensity": 0.0,
        "confidence": 0.2, "reason": "本轮缺少可靠的情绪证据",
    }]}
    current_relationship = state["relationship"]
    relationship_fallback = {
        "changes": [], "suggested_level": current_relationship["current_level"],
        "stage_reason": "证据不足，保持当前阶段", "serious_event": False,
        "behavior_boundaries": ["保持当前关系阶段对应的自然边界"],
    }
    result, warnings, llm_debug = _run_structured_agent(state, "turn_interpreter", {
        "allowed_levels": RELATIONSHIP_LEVELS,
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "recent_emotional_state": _emotions_with_time(state.get("recent_emotions", [])),
        "current_relationship": current_relationship,
    }, {"contact_emotion": emotion_fallback, "relationship": relationship_fallback})
    contact_emotion = result.get("contact_emotion", emotion_fallback)
    relationship = result.get("relationship", relationship_fallback)
    emotions = [
        item for item in contact_emotion.get("emotions", [])
        if isinstance(item, dict) and str(item.get("subject", "对方")) == "对方"
    ]
    if not emotions:
        emotions = emotion_fallback["emotions"]
    if not isinstance(relationship, dict):
        relationship = relationship_fallback
    return {
        "contact_emotion_analysis": {"emotions": emotions[:4]},
        "relationship_analysis": relationship,
        "warnings": warnings,
        "_debug_llm": llm_debug,
    }


def node_self_emotion_update(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: update persistent self emotion state after relationship judgment."""
    previous = state.get("self_emotion_state", {})
    emotion_profile = state.get("emotion_profile", {})
    fallback = {
        "events": [{"event": "其他", "impact": 0.2, "reason": "自身情绪节点未返回可靠结构"}],
        "self_emotion_state": {
            "mood": str(previous.get("mood", "放松")),
            "mood_intensity": int(previous.get("mood_intensity", 60)),
            "energy": int(previous.get("energy", 60)),
            "dimensions": [
                {"name": key, "value": int(value)}
                for key, value in (previous.get("dimensions") or {}).items()
            ],
            "reason": "沿用上一轮 AI 情绪状态",
        },
    }
    result, warnings, llm_debug = _run_structured_agent(state, "self_emotion", {
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "contact_emotion": state.get("contact_emotion_analysis", {}),
        "previous_self_emotion_state": previous,
        "current_time": datetime.now().astimezone().isoformat(),
        "persona_baseline": {
            "base_mood": emotion_profile.get("base_mood", "放松"),
            "base_energy": emotion_profile.get("base_energy", 60),
            "base_dimensions": emotion_profile.get("base_dimensions", {}),
        },
        "relationship": state.get("relationship", {}),
        "relationship_judgment": state.get("relationship_analysis", {}),
        "persona_emotion_profile": emotion_profile,
        "persona_personality": state.get("persona_personality", {}),
        "recent_events": _events_with_time(store.list_conversation_events(state["contact_id"], 8)),
    }, fallback)
    _workflow_debug_print(state, "self_emotion update", result)
    return {"self_emotion_update": result, "warnings": warnings, "_debug_llm": llm_debug}


def _flatten_habits(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        flattened: list[tuple[str, Any]] = []
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flattened.extend(_flatten_habits(nested, path))
        return flattened
    return [(prefix, value)]


def _expand_persona_query(query: str) -> set[str]:
    terms = {query}
    for trigger, expansions in _PERSONA_QUERY_EXPANSIONS.items():
        if trigger in query:
            terms.update(expansions)
    return {term.lower() for term in terms if term}


def node_persona_habit_retrieval(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: retrieve topic-relevant Habits only. Persistence: none."""
    query = state["user_request"].lower()
    grams = {query[index:index + 2] for index in range(max(0, len(query) - 1))}
    expanded_terms = _expand_persona_query(query)
    relevant: dict[str, Any] = {}
    for key, value in _flatten_habits(state.get("persona_habits", {})):
        text = f"{key} {_json(value)}".lower()
        if query and (
            any(term in text for term in expanded_terms)
            or any(gram.strip() and gram in text for gram in grams)
        ):
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
        "available_memory_types": USER_MEMORY_CATEGORIES,
        "instruction": "扩展话题关键词和近义联想，但不要臆造事实。",
    }, fallback)
    return {"memory_plan": result, "warnings": warnings, "_debug_llm": llm_debug}


def node_memory_retrieval(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: execute Memory Plan against categorized storage. Persistence: none."""
    requests = state.get("memory_plan", {}).get("requests", [])
    retrieved = store.search_memories_by_plan(state["contact_id"], requests)
    labeled = [
        {
            **item,
            "owner_type": item.get("owner_type", MEMORY_OWNER_CONTACT),
        }
        for item in retrieved
    ]
    primary = [item for item in labeled if item.get("retrieval_mode") == "exact"][:6]
    background = [item for item in labeled if item.get("retrieval_mode") != "exact"][:6]
    return {"memories": labeled, "primary_memories": primary, "background_memories": background}


def node_chat_guidance(state: WorkflowState) -> dict[str, Any]:
    """Responsibility: retrieve relevant chat-skill documents. Persistence: none."""
    query = " ".join([
        state["user_request"], state["relationship"]["current_level"],
        _json(state.get("contact_emotion_analysis", {})),
    ])
    return {"guidance_chunks": guidance_retriever.search(query, limit=5)}


def node_response_composer(state: WorkflowState) -> dict[str, Any]:
    """Create a compact plan and final candidates in one shared-context call."""
    fallback = {
        "goal": "回应当前消息并自然延续对话",
        "strategy": ["先回应对方当前表达", "没有必要时不追问"],
        "structure": ["简短直接回应"],
        "memory_usage": ["只使用明确相关且已存储的记忆"],
        "persona_expression": ["遵守固定人格与表达风格"],
        "emotion_expression": ["不过度解读情绪"],
        "topic_plan": ["优先延续当前话题"],
        "length_plan": {
            "sentence_count": 1,
            "char_budget": 25,
            "allow_filler_only": True,
            "allow_double_text": False,
            "rhythm_note": "短句为主，不要把话说完",
        },
        "candidate_directives": [
            {"candidate_index": 0, "angle": "情绪回应", "length_hint": "短", "segment_count": 1,
             "instruction": "先接住对方此刻的状态，不解释不建议"},
            {"candidate_index": 1, "angle": "极简附和", "length_hint": "极短", "segment_count": 1,
             "instruction": "只用几个字回应，不补充任何信息"},
            {"candidate_index": 2, "angle": "轻反问", "length_hint": "短", "segment_count": 1,
             "instruction": "顺着说一句，再抛一个自然的小问题"},
        ],
        "constraints": ["不捏造事实", "不越过关系行为边界"],
        "candidates": ["...", "...", "..."],
    }
    result, warnings, llm_debug = _run_structured_agent(state, "response_composer", {
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "persona": {"core": state.get("persona_core", {}), "personality": state.get("persona_personality", {})},
        "relevant_habits": state.get("relevant_habits", {}),
        "contact_emotion": _primary_contact_emotion_summary(state),
        "self_emotion_state": _self_emotion_state_summary(state),
        "relationship": {
            "current_level": state.get("relationship", {}).get("current_level"),
            "trust": state.get("relationship", {}).get("trust"),
            "familiarity": state.get("relationship", {}).get("familiarity"),
            "intimacy": state.get("relationship", {}).get("intimacy"),
            "attraction": state.get("relationship", {}).get("attraction"),
            "attachment": state.get("relationship", {}).get("attachment"),
            "comfort": state.get("relationship", {}).get("comfort"),
        },
        "relationship_judgment": {
            "suggested_level": state.get("relationship_analysis", {}).get("suggested_level"),
            "stage_reason": state.get("relationship_analysis", {}).get("stage_reason"),
            "behavior_boundaries": state.get("relationship_analysis", {}).get("behavior_boundaries", []),
        },
        "memories": _memory_summary(state.get("primary_memories") or state.get("memories", []), limit=4),
        "background_memories": _memory_summary(state.get("background_memories", []), limit=2),
        "chat_skill_documents": state.get("guidance_chunks", [])[:2],
    }, fallback)
    candidates = [_normalize_segments(str(item)) for item in result.get("candidates", [])][:3]
    composition = {
        key: result.get(key, fallback[key])
        for key in (
            "goal", "strategy", "structure", "memory_usage", "persona_expression",
            "emotion_expression", "topic_plan", "length_plan", "candidate_directives", "constraints",
        )
    }
    _workflow_debug_print(state, "response_composer", {
        "goal": composition.get("goal", ""),
        "strategy": result.get("strategy", fallback["strategy"]),
        "structure": composition.get("structure", []),
        "memory_usage": result.get("memory_usage", fallback["memory_usage"]),
        "persona_expression": result.get("persona_expression", fallback["persona_expression"]),
        "emotion_expression": result.get("emotion_expression", fallback["emotion_expression"]),
        "topic_plan": composition.get("topic_plan", []),
        "length_plan": composition.get("length_plan", {}),
        "candidate_directives": composition.get("candidate_directives", []),
        "constraints": composition.get("constraints", []),
        "candidates": candidates,
    })
    return {
        "response_composition": composition,
        "strategy": str(result.get("goal", "")),
        "candidates": candidates,
        "warnings": warnings,
        "_debug_llm": llm_debug,
    }


def node_reply_critic(state: WorkflowState) -> dict[str, Any]:
    """Review candidates and inline-revise rejected items before the next review."""
    fallback = {
        "reviews": [{
            "candidate_index": index, "ai_taste": 0, "interview_taste": 0,
            "empathy_excess": 0, "naturalness": 8, "persona_consistent": True,
            "memory_consistent": True, "passed": True, "issues": [],
            "rewrite_instruction": "", "revised_candidate": "",
        } for index in range(3)],
        "passed": True,
    }
    review, warnings, llm_debug = _run_structured_agent(state, "critic", {
        "latest_message": state["user_request"],
        "recent_conversation_with_time": _history_with_time(state.get("history", [])),
        "persona": {"core": state.get("persona_core", {}), "personality": state.get("persona_personality", {})},
        "relevant_memories": state.get("primary_memories") or state.get("memories", []),
        "response_plan": state.get("response_composition", {}),
        "candidates": state["candidates"],
        "review_dimensions": ["style", "persona", "memory"],
    }, fallback)
    attempts = state.get("critic_attempts", 0) + 1
    history = [*state.get("critic_analysis", {}).get("reviews", []), review]
    candidate_reviews = review.get("reviews", [])
    candidates = list(state.get("candidates", []))
    revised_indexes: list[int] = []
    for item in candidate_reviews:
        if not isinstance(item, dict) or bool(item.get("passed", True)):
            continue
        candidate_index = item.get("candidate_index")
        revised = item.get("revised_candidate")
        if isinstance(candidate_index, int) and candidate_index in {0, 1, 2} and isinstance(revised, str) and revised.strip():
            candidates[candidate_index] = _normalize_segments(revised)
            revised_indexes.append(candidate_index)
    _workflow_debug_print(state, "critic analysis", review)
    result: dict[str, Any] = {
        "critic_attempts": attempts,
        "critic_analysis": {
            "reviews": history,
            "passed": bool(review.get("passed", True)),
            "revised_candidate_indexes": sorted(set(revised_indexes)),
        },
        "candidates": candidates,
        "warnings": warnings,
        "_debug_llm": llm_debug,
    }
    if not review.get("passed", True) and attempts >= CRITIC_MAX_ATTEMPTS:
        result["warnings"] = [*warnings, "Reply Critic 达到最大复评次数，已返回最后一版候选并记录告警。"]
    return result


def route_after_critic(state: WorkflowState) -> str:
    if state.get("critic_analysis", {}).get("passed", True) or state.get("critic_attempts", 0) >= CRITIC_MAX_ATTEMPTS:
        return "critic_complete"
    return "critic"


def node_user_memory_recognition(state: WorkflowState) -> dict[str, Any]:
    """Recognize durable facts from the current real user message and existing memory only."""
    fallback = {"memories": []}
    existing_memories = [
        item for item in store.search_memories(state["contact_id"], state["user_request"], limit=12)
        if item.get("retrieval_mode") == "exact"
    ]
    result, warnings, llm_debug = _run_structured_agent(state, "user_mem_recognition", {
        "latest_real_user_message": state["user_request"],
        "existing_relevant_memories": _reflection_memories_summary(existing_memories, limit=12),
        "allowed_memory_types": USER_MEMORY_CATEGORIES,
        "instruction": "只基于本轮真实用户消息判断；已有记忆不重复输出，不使用历史聊天或候选回复。",
    }, fallback)
    return {"user_memory_recognition": result, "warnings": warnings, "_debug_llm": llm_debug}


def node_content_save(state: WorkflowState) -> dict[str, Any]:
    """Collect final candidates; all DB persistence is deferred to /api/chat/confirm."""
    return {"candidates": state.get("candidates", [])}


def node_critic_complete(_: WorkflowState) -> dict[str, Any]:
    """Mark the end of the bounded critic loop for the final graph barrier."""
    return {}


def build_graph():
    graph = StateGraph(WorkflowState)
    graph.add_node("context", _trace_node("context", node_load_context))
    graph.add_node("turn_interpreter", _trace_node("turn_interpreter", node_turn_interpreter))
    graph.add_node("persona_habit_retrieval", _trace_node("persona_habit_retrieval", node_persona_habit_retrieval))
    graph.add_node("self_emotion", _trace_node("self_emotion", node_self_emotion_update))
    graph.add_node("memory_planner", _trace_node("memory_planner", node_memory_planner))
    graph.add_node("memory_retrieval", _trace_node("memory_retrieval", node_memory_retrieval))
    graph.add_node("guidance", _trace_node("guidance", node_chat_guidance))
    graph.add_node("response_composer", _trace_node("response_composer", node_response_composer))
    graph.add_node("critic", _trace_node("critic", node_reply_critic))
    graph.add_node("user_mem_recognition", _trace_node("user_mem_recognition", node_user_memory_recognition))
    graph.add_node("critic_complete", _trace_node("critic_complete", node_critic_complete))
    graph.add_node("save", _trace_node("save", node_content_save))
    graph.set_entry_point("context")
    graph.add_edge("context", "turn_interpreter")
    graph.add_edge("context", "persona_habit_retrieval")
    graph.add_edge("context", "memory_planner")
    graph.add_edge("context", "user_mem_recognition")
    graph.add_edge("memory_planner", "memory_retrieval")
    graph.add_edge("turn_interpreter", "self_emotion")
    graph.add_edge("turn_interpreter", "guidance")
    graph.add_edge(["self_emotion", "guidance", "persona_habit_retrieval", "memory_retrieval"], "response_composer")
    graph.add_edge("response_composer", "critic")
    graph.add_conditional_edges("critic", route_after_critic, {
        "critic": "critic", "critic_complete": "critic_complete",
    })
    graph.add_edge(["critic_complete", "user_mem_recognition"], "save")
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