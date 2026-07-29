import unittest

from pydantic import ValidationError

from src.agents import EmotionAnalysis, ResponseCandidates, get_agent


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


if __name__ == "__main__":
    unittest.main()
