import unittest

from pydantic import ValidationError

from src.agents import DialoguePolicyAnalysis, EmotionAnalysis, ReplyCriticAnalysis, ResponseCandidates, get_agent


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
                    "emotion": "疲惫",
                    "intensity": 2,
                    "confidence": 0.9,
                    "reason": "无效强度",
                }]
            })

    def test_response_requires_exactly_three_candidates(self):
        with self.assertRaises(ValidationError):
            ResponseCandidates(candidates=["只有一条"])
        valid = ResponseCandidates(candidates=["一", "二", "三"])
        self.assertEqual(len(valid.candidates), 3)

    def test_dialogue_policy_supports_silence_and_dynamic_question_probability(self):
        policy = DialoguePolicyAnalysis.model_validate({
            "primary_mode": "简短回应",
            "candidate_modes": ["简短回应", "分享自己", "暂不回复"],
            "question_probability": 0.1,
            "question_budget": 0,
            "should_resist": False,
            "rationale": "最近问句较多",
        })
        self.assertEqual(policy.candidate_modes[-1], "暂不回复")

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


if __name__ == "__main__":
    unittest.main()
