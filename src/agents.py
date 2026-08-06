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


EmotionName = Literal[
    "开心", "兴奋", "满足", "感动", "安心", "平静", "放松", "期待", "好奇",
    "紧张", "焦虑", "害羞", "尴尬", "困惑", "犹豫", "疲惫", "无聊", "失望",
    "难过", "委屈", "吃醋", "生气", "沮丧", "内疚", "想念", "其他", "unknown",
]
MoodName = Literal[
    "放松", "开心", "专注", "孤单", "疲惫", "焦虑", "低落",
]
ShortEmotionDimension = Literal[
    "喜悦", "惊喜", "安心", "好奇", "期待", "害羞", "紧张", "焦虑", "生气",
    "失落", "思念", "依恋",
]
RelationshipLevel = Literal["陌生人", "认识", "朋友", "亲密朋友", "暧昧", "恋人"]
RelationshipDimension = Literal["trust", "familiarity", "intimacy", "attraction", "attachment", "comfort"]
MemoryCategory = Literal["episodic", "semantic", "emotional", "shared_experience"]
CandidateAngle = Literal[
    "情绪回应", "轻反问", "玩笑吐槽", "极简附和", "自我暴露", "具体建议", "话题延展", "暂不回复",
]
CandidateLengthHint = Literal["极短", "短", "中"]
ConversationEventType = Literal[
    "好消息", "坏消息", "道歉", "感谢", "夸奖", "失联", "冷淡", "冲突", "和解",
    "示好", "试探", "边界确认", "表达想念", "邀约", "拒绝", "求安慰", "求建议",
    "日常分享", "工作压力", "学业压力", "家庭压力", "健康困扰", "节日问候", "其他",
]


class EmotionItem(BaseModel):
    """One evidence-based emotional state."""

    subject: Literal["我", "对方"] = Field(description="情绪主体")
    emotion: EmotionName = Field(description="当前主要情绪")
    intensity: float = Field(ge=0, le=1, description="情绪强度，0 到 1")
    confidence: float = Field(ge=0, le=1, description="判断置信度，0 到 1")
    reason: str = Field(description="简短的文本证据；无证据时明确说明")


class EmotionAnalysis(BaseModel):
    """User-only emotion detection with legacy fields preserved."""

    emotions: list[EmotionItem] = Field(default_factory=list, max_length=4)


class ConversationEvent(BaseModel):
    """A compact turn-level event extracted before AI emotion update."""

    event: ConversationEventType
    impact: float = Field(ge=0, le=1, description="事件影响强度，0 到 1")
    reason: str = Field(description="事件判断依据")


class AIDimensionState(BaseModel):
    """One short-lived AI emotion dimension score."""

    name: ShortEmotionDimension
    value: int = Field(ge=0, le=100, description="短期情绪数值，0 到 100")


class AIEmotionState(BaseModel):
    """Persistent AI affective state used by later dialogue planning."""

    mood: MoodName
    mood_intensity: int = Field(ge=0, le=100, description="mood 的强度，0 到 100，越高越强烈")
    energy: int = Field(ge=0, le=100)
    dimensions: list[AIDimensionState] = Field(default_factory=list, max_length=8)
    reason: str = Field(description="本轮更新的简短依据")


class AIEmotionUpdateAnalysis(BaseModel):
    """AI emotion update output combining event extraction and persistent state."""

    events: list[ConversationEvent] = Field(default_factory=list, max_length=4)
    ai_emotion: AIEmotionState


class RelationshipChange(BaseModel):
    """One evidence-based score adjustment proposed by the relationship model."""

    dimension: RelationshipDimension
    delta: int = Field(ge=-10, le=10)
    reason: str


class RelationshipAnalysis(BaseModel):
    """Relationship score events, stage judgment, and turn-level behavior boundaries."""

    changes: list[RelationshipChange] = Field(default_factory=list, max_length=6)
    suggested_level: RelationshipLevel
    stage_reason: str
    serious_event: bool = False
    behavior_boundaries: list[str] = Field(default_factory=list, max_length=8)


class MemoryPlanItem(BaseModel):
    """One expanded retrieval intent produced before local memory search."""

    memory_type: MemoryCategory
    query: str
    keywords: list[str] = Field(default_factory=list, max_length=10)
    priority: int = Field(ge=1, le=5)
    top_k: int = Field(ge=1, le=20)
    time_range: str = "all"
    reason: str


class MemoryPlan(BaseModel):
    """Topic-aware and associative memory retrieval plan."""

    requests: list[MemoryPlanItem] = Field(default_factory=list, max_length=8)


class LengthPlan(BaseModel):
    """Turn-level rhythm budget so replies read like chat instead of prose."""

    sentence_count: int = Field(default=2, ge=1, le=3, description="本轮建议句子数")
    char_budget: int = Field(default=30, ge=2, le=60, description="整轮中文字符上限")
    openness: float = Field(default=0.4, ge=0, le=1, description="留白比例，越高说得越少")
    allow_filler_only: bool = Field(default=True, description="是否允许没有信息量的纯语气回应")
    rhythm_note: str = Field(default="", description="本轮节奏的一句话说明")


class CandidateDirective(BaseModel):
    """Per-candidate writing angle so the three options differ by design."""

    candidate_index: int = Field(ge=0, le=2)
    angle: CandidateAngle
    length_hint: CandidateLengthHint
    instruction: str = Field(description="这条候选怎么写、不写什么")


class DialogueComposerAnalysis(BaseModel):
    """A response plan; this node never writes the final reply."""

    goal: str
    strategy: list[str] = Field(default_factory=list, max_length=8)
    structure: list[str] = Field(default_factory=list, max_length=8)
    memory_usage: list[str] = Field(default_factory=list, max_length=8)
    persona_expression: list[str] = Field(default_factory=list, max_length=8)
    emotion_expression: list[str] = Field(default_factory=list, max_length=8)
    topic_plan: list[str] = Field(default_factory=list, max_length=8)
    length_plan: LengthPlan = Field(default_factory=LengthPlan)
    candidate_directives: list[CandidateDirective] = Field(min_length=3, max_length=3)
    constraints: list[str] = Field(default_factory=list, max_length=12)


class CandidateCritique(BaseModel):
    """Human-likeness scores for one generated candidate."""

    candidate_index: int = Field(ge=0, le=2)
    ai_taste: float = Field(ge=0, le=10, description="AI味/客服味，越低越好")
    interview_taste: float = Field(ge=0, le=10, description="采访味，越低越好")
    empathy_excess: float = Field(ge=0, le=10, description="空洞或过度共情，越低越好")
    naturalness: float = Field(ge=0, le=10, description="长度与措辞自然度，越高越好")
    persona_consistent: bool = True
    memory_consistent: bool = True
    passed: bool
    issues: list[str] = Field(default_factory=list, max_length=5)
    rewrite_instruction: str = ""


class ReplyCriticAnalysis(BaseModel):
    """Post-generation review used to trigger bounded rewrites."""

    candidates: list[CandidateCritique] = Field(min_length=3, max_length=3)
    passed: bool
    rewrite_candidate_indexes: list[int] = Field(default_factory=list, max_length=3)


class ResponseCandidates(BaseModel):
    """Exactly three editable candidate messages."""

    candidates: list[str] = Field(min_length=3, max_length=3)


class MemoryItem(BaseModel):
    """A durable fact extracted from real conversation only."""

    category: MemoryCategory
    content: str
    sensitive: bool = False


class HabitUpdate(BaseModel):
    """One automatically applicable persona habit update."""

    key: str
    value: str
    reason: str


class CoreUpdateProposal(BaseModel):
    """A core identity change that requires explicit human confirmation."""

    key: str
    value: str
    reason: str


class ReflectionAnalysis(BaseModel):
    """Post-response decisions about durable memory and persona updates."""

    memories: list[MemoryItem] = Field(default_factory=list, max_length=8)
    habit_updates: list[HabitUpdate] = Field(default_factory=list, max_length=6)
    core_update_proposals: list[CoreUpdateProposal] = Field(default_factory=list, max_length=4)


class ProactiveDraft(BaseModel):
    """A draft that is never sent automatically."""

    draft: str
    reason: str


AGENT_SCHEMAS: dict[str, type[BaseModel]] = {
    "emotion": EmotionAnalysis,
    "ai_emotion": AIEmotionUpdateAnalysis,
    "relationship": RelationshipAnalysis,
    "memory_planner": MemoryPlan,
    "dialogue_composer": DialogueComposerAnalysis,
    "prompt_composer": ResponseCandidates,
    "critic": ReplyCriticAnalysis,
    "reflection": ReflectionAnalysis,
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
        model=get_model(provider_name, agent_name=agent_name),
        tools=[],
        system_prompt=load_prompt(agent_name),
        response_format=_structured_output(schema),
        middleware=_middleware(),
        name=f"{agent_name}_agent",
        debug=False,
    )
