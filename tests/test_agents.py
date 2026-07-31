import unittest

from pydantic import ValidationError

from src.agents import (
    AIEmotionUpdateAnalysis,
    DialogueComposerAnalysis,
    EmotionAnalysis,
    MemoryPlan,
    RelationshipAnalysis,
    ReplyCriticAnalysis,
    ResponseCandidates,
    get_agent,
)


class AgentHarnessTests(unittest.TestCase):
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

    def test_ai_emotion_update_schema_supports_multidimensional_state(self):
        value = AIEmotionUpdateAnalysis.model_validate({
            "events": [{"event": "好消息", "impact": 0.8, "reason": "用户分享升职"}],
            "ai_emotion": {
                "mood": "开心",
                "energy": 72,
                "dimensions": [
                    {"name": "喜悦", "value": 45},
                    {"name": "好奇", "value": 68},
                ],
                "reason": "关系较近，因此更容易被带动",
            },
        })
        self.assertEqual(value.ai_emotion.mood, "开心")
        self.assertEqual(value.ai_emotion.dimensions[1].name, "好奇")

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
                "passed": True,
                "issues": [],
            } for index in range(3)],
            "passed": True,
            "rewrite_instruction": "",
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
            "constraints": ["不连续追问"],
        })
        self.assertEqual(composer.goal, "回应近况并自然延续话题")


if __name__ == "__main__":
    unittest.main()
