import unittest

from pydantic import ValidationError

from src.agents import (
    MemoryPlan,
    ReplyCriticAnalysis,
    SelfEmotionUpdateAnalysis,
    TurnInterpreterAnalysis,
    ResponseComposerAnalysis,
    clear_agent_cache,
    get_agent,
)


class AgentHarnessTests(unittest.TestCase):
    def test_clear_agent_cache_is_available(self):
        clear_agent_cache()

    def test_agents_are_cached_per_provider(self):
        self.assertIs(
            get_agent("turn_interpreter", "default"),
            get_agent("turn_interpreter", "default"),
        )

    def test_structured_response_schema_validates_ranges(self):
        value = TurnInterpreterAnalysis.model_validate({
            "contact_emotion": {"emotions": [{
                "subject": "对方",
                "emotion": "疲惫",
                "intensity": 0.8,
                "confidence": 0.9,
                "reason": "对方明确说今天很累",
            }]},
            "relationship": {
                "changes": [], "suggested_level": "认识", "stage_reason": "无关系变化",
                "behavior_boundaries": ["保持自然称呼"],
            },
        })
        self.assertEqual(value.contact_emotion.emotions[0].emotion, "疲惫")
        with self.assertRaises(ValidationError):
            TurnInterpreterAnalysis.model_validate({
                "contact_emotion": {"emotions": [{
                    "subject": "对方",
                    "emotion": "unknown",
                    "intensity": 2,
                    "confidence": 0.9,
                    "reason": "无效强度",
                }]},
                "relationship": {
                    "changes": [], "suggested_level": "认识", "stage_reason": "无关系变化",
                    "behavior_boundaries": [],
                },
            })

    def test_self_emotion_update_schema_supports_multidimensional_state(self):
        value = SelfEmotionUpdateAnalysis.model_validate({
            "events": [{"event": "日常分享", "impact": 0.8, "reason": "用户分享升职"}],
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

    def test_response_composer_requires_exactly_three_candidates(self):
        with self.assertRaises(ValidationError):
            ResponseComposerAnalysis.model_validate({
                "goal": "回应", "candidate_directives": [], "candidates": ["只有一条"],
            })

    def test_critic_schema_has_three_candidate_reviews(self):
        review = ReplyCriticAnalysis.model_validate({
            "reviews": [{
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
                "revised_candidate": "",
            } for index in range(3)],
            "passed": True,
        })
        self.assertTrue(review.passed)

    def test_upgraded_pipeline_schemas_validate(self):
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

        composer = ResponseComposerAnalysis.model_validate({
            "goal": "回应近况并自然延续话题",
            "structure": ["短回应", "相关分享"],
            "topic_plan": ["项目进度"],
            "length_plan": {
                "sentence_count": 1,
                "char_budget": 20,
                "allow_filler_only": True,
                "allow_double_text": False,
                "rhythm_note": "只回半句",
            },
            "candidate_directives": [
                {"candidate_index": 0, "angle": "情绪回应", "length_hint": "短", "segment_count": 2, "instruction": "先接住状态"},
                {"candidate_index": 1, "angle": "极简附和", "length_hint": "极短", "segment_count": 1, "instruction": "几个字带过"},
                {"candidate_index": 2, "angle": "轻反问", "length_hint": "短", "segment_count": 1, "instruction": "顺着问一句"},
            ],
            "constraints": ["不连续追问"],
            "candidates": ["可以啊", "你这进度挺快", "听着还行"],
            "debug": {
                "strategy": ["先回应"],
                "memory_usage": ["只使用确认过的创业记忆"],
                "persona_expression": ["保持克制"],
                "emotion_expression": ["轻微关心"],
            },
        })
        self.assertEqual(composer.goal, "回应近况并自然延续话题")
        self.assertEqual(composer.length_plan.char_budget, 20)
        self.assertEqual(
            [item.length_hint for item in composer.candidate_directives], ["短", "极短", "短"],
        )
        self.assertEqual(
            [item.segment_count for item in composer.candidate_directives], [2, 1, 1],
        )


if __name__ == "__main__":
    unittest.main()
