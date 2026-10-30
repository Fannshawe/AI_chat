import os
import unittest
from unittest.mock import patch

from langchain.agents.structured_output import ProviderStrategy
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from src.config import get_agent_model_provider, get_model_providers
from src.llm_service import OpenAICompatibleChatModel, _build_model, get_model
from src.agents import (
    DialogueComposerAnalysis,
    EmotionAnalysis,
    MemoryPlan,
    RelationshipAnalysis,
    ReplyCriticAnalysis,
    ResponseCandidates,
    SelfEmotionUpdateAnalysis,
    clear_agent_cache,
    get_agent,
)


class TemperatureConfigTests(unittest.TestCase):
    def setUp(self):
        env_patch = patch.dict(os.environ, {
            "AGENT_PROMPT_COMPOSER_MODEL": "test-model",
            "AGENT_PROMPT_COMPOSER_BASE_URL": "https://example.test/v1",
            "AGENT_PROMPT_COMPOSER_API_KEY": "test-key",
        }, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_unconfigured_temperature_defaults_to_point_seven(self):
        self.assertEqual(get_model_providers()["default"].temperature, 0.7)
        self.assertEqual(get_agent_model_provider("prompt_composer").temperature, 0.7)

    def test_global_temperature_is_inherited(self):
        os.environ.update({
            "DEFAULT_TEMPERATURE": "0.8",
            "DEFAULT_MODEL": "test-model",
            "DEFAULT_BASE_URL": "https://example.test/v1",
            "DEFAULT_API_KEY": "test-key",
        })
        self.assertEqual(get_model_providers()["default"].temperature, 0.8)
        self.assertEqual(get_model(agent_name="prompt_composer").temperature, 0.8)
        self.assertEqual(get_model().temperature, 0.8)

    def test_agent_temperature_overrides_global_including_zero(self):
        os.environ["DEFAULT_TEMPERATURE"] = "0.8"
        os.environ["AGENT_PROMPT_COMPOSER_TEMPERATURE"] = "0"
        provider = get_agent_model_provider("prompt_composer")
        self.assertEqual(provider.temperature, 0.0)
        self.assertEqual(provider.public_dict()["temperature"], 0.0)
        self.assertEqual(get_model(agent_name="prompt_composer").temperature, 0.0)

    def test_blank_agent_temperature_inherits_global(self):
        os.environ["DEFAULT_TEMPERATURE"] = "0.8"
        os.environ["AGENT_PROMPT_COMPOSER_TEMPERATURE"] = "  "
        self.assertEqual(get_agent_model_provider("prompt_composer").temperature, 0.8)

    def test_invalid_temperatures_are_rejected(self):
        for name in ("DEFAULT_TEMPERATURE", "AGENT_PROMPT_COMPOSER_TEMPERATURE"):
            for value in ("invalid", "nan", "inf", "-0.1", "2.1"):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    with self.assertRaisesRegex(ValueError, name):
                        get_agent_model_provider("prompt_composer")

    def test_temperature_is_part_of_model_cache_key(self):
        os.environ["AGENT_PROMPT_COMPOSER_TEMPERATURE"] = "0.3"
        first = get_model(agent_name="prompt_composer")
        self.assertIs(first, get_model(agent_name="prompt_composer"))
        os.environ["AGENT_PROMPT_COMPOSER_TEMPERATURE"] = "0.9"
        second = get_model(agent_name="prompt_composer")
        self.assertIsNot(first, second)
        self.assertEqual(second.temperature, 0.9)


class CompatibleModelPayloadTests(unittest.TestCase):
    def setUp(self):
        self.model = OpenAICompatibleChatModel(
            model="qwen-plus-character",
            api_key="test-key",
            base_url="https://example.test/v1",
        )
        self.messages = [HumanMessage(content="hello")]

    def test_provider_strategy_omits_empty_tools(self):
        response_kwargs = ProviderStrategy(
            schema=ResponseCandidates, strict=True,
        ).to_model_kwargs()
        bound = self.model.bind_tools([], **response_kwargs)
        payload = bound.bound._get_request_payload(self.messages, **bound.kwargs)
        original_payload = ChatOpenAI._get_request_payload(
            self.model, self.messages, **bound.kwargs,
        )
        self.assertEqual(original_payload["tools"], [])
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertEqual(payload["response_format"], original_payload["response_format"])

    def test_empty_tools_also_omit_tool_choice(self):
        payload = self.model._get_request_payload(
            self.messages, tools=[], tool_choice="auto",
        )
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_nonempty_tools_are_preserved(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        payload = self.model._get_request_payload(
            self.messages, tools=tools, tool_choice="auto",
        )
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["tool_choice"], "auto")

    def test_custom_endpoint_uses_compatible_model(self):
        model = _build_model(
            "payload-test", "qwen-plus-character", "https://example.test/v1",
            "test-key", False,
        )
        self.assertIsInstance(model, OpenAICompatibleChatModel)
        self.assertEqual(model.extra_body, {"enable_thinking": False})


class AgentHarnessTests(unittest.TestCase):
    def test_clear_agent_cache_is_available(self):
        clear_agent_cache()

    def test_agents_are_cached_per_provider(self):
        self.assertIs(get_agent("emotion", "default"), get_agent("emotion", "default"))

    def test_structured_response_schema_validates_ranges(self):
        value = EmotionAnalysis.model_validate({
            "emotions": [{
                "subject": "对方",
                "emotion": "疲惫",
                "intensity": 0.8,
                "confidence": 0.9,
                "reason": "对方明确说今天很累",
            }]
        })
        self.assertEqual(value.emotions[0].emotion, "疲惫")
        with self.assertRaises(ValidationError):
            EmotionAnalysis.model_validate({
                "emotions": [{
                    "subject": "对方",
                    "emotion": "unknown",
                    "intensity": 2,
                    "confidence": 0.9,
                    "reason": "无效强度",
                }]
            })

    def test_self_emotion_update_schema_supports_multidimensional_state(self):
        value = SelfEmotionUpdateAnalysis.model_validate({
            "events": [{"event": "好消息", "impact": 0.8, "reason": "用户分享升职"}],
            "self_emotion_state": {
                "mood": "开心",
                "mood_intensity": 65,
                "energy": 72,
                "dimensions": [
                    {"name": "喜悦", "value": 45},
                    {"name": "好奇", "value": 68},
                ],
                "reason": "关系较近，因此更容易被带动",
            },
        })
        self.assertEqual(value.self_emotion_state.mood, "开心")
        self.assertEqual(value.self_emotion_state.dimensions[1].name, "好奇")

    def test_response_requires_exactly_three_candidates(self):
        with self.assertRaises(ValidationError):
            ResponseCandidates(candidates=["只有一条"])
        valid = ResponseCandidates(candidates=["一", "二", "三"])
        self.assertEqual(len(valid.candidates), 3)

    def test_critic_schema_has_three_candidate_reviews(self):
        review = ReplyCriticAnalysis.model_validate({
            "candidates": [{
                "candidate_index": index,
                "ai_taste": 2,
                "interview_taste": 1,
                "empathy_excess": 1,
                "naturalness": 8,
                "persona_consistent": True,
                "memory_consistent": True,
                "passed": True,
                "issues": [],
                "rewrite_instruction": "",
            } for index in range(3)],
            "passed": True,
            "rewrite_candidate_indexes": [],
        })
        self.assertTrue(review.passed)

    def test_upgraded_pipeline_schemas_validate(self):
        relationship = RelationshipAnalysis.model_validate({
            "changes": [{"dimension": "trust", "delta": 3, "reason": "对方兑现了承诺"}],
            "suggested_level": "朋友",
            "stage_reason": "互动稳定，但没有越级推进关系",
            "serious_event": False,
            "behavior_boundaries": ["可以自然分享近况", "不使用恋人式称呼"],
        })
        self.assertEqual(relationship.changes[0].delta, 3)

        plan = MemoryPlan.model_validate({
            "requests": [{
                "memory_type": "semantic",
                "query": "用户对人工智能和创业的长期兴趣",
                "keywords": ["AI", "创业", "项目"],
                "priority": 5,
                "top_k": 4,
                "time_range": "all",
                "reason": "当前话题可能延续用户的长期项目",
            }]
        })
        self.assertEqual(plan.requests[0].memory_type, "semantic")

        composer = DialogueComposerAnalysis.model_validate({
            "goal": "回应近况并自然延续话题",
            "strategy": ["先回应"],
            "structure": ["短回应", "相关分享"],
            "memory_usage": ["只使用确认过的创业记忆"],
            "persona_expression": ["保持克制"],
            "emotion_expression": ["轻微关心"],
            "topic_plan": ["项目进度"],
            "length_plan": {
                "char_budget": 20,
                "allow_filler_only": True,
                "rhythm_note": "只回半句",
            },
            "candidate_directives": [
                {"candidate_index": 0, "angle": "情绪回应", "length_hint": "短", "segment_count": 2, "instruction": "先接住状态"},
                {"candidate_index": 1, "angle": "极简附和", "length_hint": "极短", "segment_count": 1, "instruction": "几个字带过"},
                {"candidate_index": 2, "angle": "轻反问", "length_hint": "短", "segment_count": 1, "instruction": "顺着问一句"},
            ],
            "constraints": ["不连续追问"],
        })
        self.assertEqual(composer.goal, "回应近况并自然延续话题")
        self.assertEqual(composer.length_plan.char_budget, 20)
        self.assertNotIn("sentence_count", composer.length_plan.model_dump())
        self.assertNotIn(
            "sentence_count", composer.length_plan.model_json_schema()["properties"],
        )
        self.assertNotIn("allow_double_text", composer.length_plan.model_dump())
        self.assertNotIn(
            "allow_double_text", composer.length_plan.model_json_schema()["properties"],
        )
        self.assertEqual(
            [item.length_hint for item in composer.candidate_directives], ["短", "极短", "短"],
        )
        self.assertEqual(
            composer.candidate_directives[0].model_json_schema()["properties"]["length_hint"]["enum"],
            ["极短", "短", "中", "长"],
        )
        self.assertEqual(
            [item.segment_count for item in composer.candidate_directives], [2, 1, 1],
        )


if __name__ == "__main__":
    unittest.main()
