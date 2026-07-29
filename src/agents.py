"""LangChain v1 agents, structured response schemas, and middleware.

Each LLM-backed LangGraph node calls one of these ``create_agent`` instances
with ``agent.invoke(...)``. Agents are cached per configured model provider so
the administration page can switch providers without rebuilding the outer graph.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ModelRetryMiddleware
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

from .llm_service import get_model
from .prompt_loader import load_prompt


EmotionName = Literal["开心", "委屈", "吃醋", "疲惫", "期待", "紧张", "害羞", "平静", "其他"]
RelationshipLevel = Literal["陌生人", "认识", "朋友", "暧昧", "恋人", "长期伴侣"]
MemoryCategory = Literal["人物事实与偏好", "共同经历与纪念日", "情绪事件与冲突", "承诺和待办事项", "敏感信息"]


class EmotionItem(BaseModel):
    """One evidence-based emotional state."""

    subject: Literal["我", "对方"] = Field(description="情绪主体")
    emotion: EmotionName = Field(description="当前主要情绪")
    intensity: float = Field(ge=0, le=1, description="情绪强度，0 到 1")
    confidence: float = Field(ge=0, le=1, description="判断置信度，0 到 1")
    reason: str = Field(description="简短的文本证据；无证据时明确说明")


class EmotionAnalysis(BaseModel):
    """Continuous emotional analysis for both participants."""

    emotions: list[EmotionItem] = Field(default_factory=list, max_length=4)


class RelationshipAnalysis(BaseModel):
    """A relationship-stage suggestion that still requires human approval."""

    suggested_level: RelationshipLevel
    confidence: float = Field(ge=0, le=1)
    reason: str


class PersonaAnalysis(BaseModel):
    """Persona constraints consumed by the final response agent."""

    style_instruction: str
    counterpart_observations: list[str] = Field(default_factory=list, max_length=8)
    conflicts: list[str] = Field(default_factory=list, max_length=5)


class GuidanceAnalysis(BaseModel):
    """An actionable internal response strategy."""

    strategy: str
    tone: str
    avoid: list[str] = Field(default_factory=list, max_length=8)
    document_basis: list[str] = Field(default_factory=list, max_length=5)


class ResponseCandidates(BaseModel):
    """Exactly three editable candidate messages."""

    candidates: list[str] = Field(min_length=3, max_length=3)


class MemoryItem(BaseModel):
    """A durable fact extracted from real conversation only."""

    category: MemoryCategory
    content: str
    sensitive: bool = False


class MemoryAnalysis(BaseModel):
    """Long-term memories worth persisting."""

    memories: list[MemoryItem] = Field(default_factory=list, max_length=8)


class ProactiveDraft(BaseModel):
    """A draft that is never sent automatically."""

    draft: str
    reason: str


AGENT_SCHEMAS: dict[str, type[BaseModel]] = {
    "emotion": EmotionAnalysis,
    "relationship": RelationshipAnalysis,
    "persona": PersonaAnalysis,
    "guidance": GuidanceAnalysis,
    "response": ResponseCandidates,
    "memory": MemoryAnalysis,
    "proactive": ProactiveDraft,
}


def _structured_output(schema: type[BaseModel]) -> ToolStrategy:
    """Use tool-calling output for OpenAI-compatible endpoints without profiles."""
    return ToolStrategy(
        schema=schema,
        tool_message_content="结构化结果已验证。",
        handle_errors="输出不符合结构要求，请仅修正字段和值后重试，不要添加解释。",
    )


def _middleware():
    """Bound retries and calls so one node cannot loop or overspend."""
    return [
        ModelRetryMiddleware(
            max_retries=2,
            initial_delay=0.5,
            backoff_factor=2.0,
            max_delay=4.0,
            on_failure="error",
        ),
        ModelCallLimitMiddleware(run_limit=4, exit_behavior="error"),
    ]


@lru_cache(maxsize=32)
def get_agent(agent_name: str, provider_name: str = "default"):
    """Return one named ``create_agent`` harness for a model provider."""
    schema = AGENT_SCHEMAS.get(agent_name)
    if schema is None:
        raise KeyError(f"未知 Agent: {agent_name}")
    return create_agent(
        model=get_model(provider_name),
        tools=[],
        system_prompt=load_prompt(agent_name),
        response_format=_structured_output(schema),
        middleware=_middleware(),
        name=f"{agent_name}_agent",
        debug=False,
    )
