from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from src.graph import build_graph
from src.storage import ChatStore


class FakeAgent:
    def __init__(self, name, responses, counts, lock):
        self.name = name
        self.responses = responses
        self.counts = counts
        self.lock = lock

    def invoke(self, _payload):
        with self.lock:
            index = self.counts.get(self.name, 0)
            self.counts[self.name] = index + 1
        configured = self.responses[self.name]
        response = configured[min(index, len(configured) - 1)] if isinstance(configured, list) else configured
        return {"structured_response": response}


def critique(passed, naturalness=8, instruction=""):
    return {
        "candidates": [{
            "candidate_index": index,
            "ai_taste": 1,
            "interview_taste": 1,
            "empathy_excess": 1,
            "naturalness": naturalness,
            "passed": passed,
            "issues": [] if passed else ["需要更自然"],
        } for index in range(3)],
        "passed": passed,
        "rewrite_instruction": instruction,
    }


class GraphPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = ChatStore(root / "chat.db", root / "uploads")
        self.store.persona_path = root / "data" / "persona.yaml"
        self.store.persona_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.persona_path.write_text(
            "core:\n  name: Lin\npersonality:\n  expression_style: concise\nhabits:\n  likes: [coffee]\n",
            encoding="utf-8",
        )
        self.store.initialize()
        self.contact_id = self.store.list_contacts()[0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def base_responses(self):
        return {
            "emotion": {"emotions": [{
                "subject": "对方", "emotion": "开心", "intensity": 0.8,
                "confidence": 0.9, "reason": "用户分享了积极近况",
            }]},
            "relationship": {
                "changes": [{"dimension": "familiarity", "delta": 2, "reason": "自然分享近况"}],
                "suggested_level": "熟人",
                "stage_reason": "保持当前阶段",
                "serious_event": False,
                "behavior_boundaries": ["保持自然称呼"],
            },
            "ai_emotion": {
                "events": [{"event": "好消息", "impact": 0.7, "reason": "用户在分享开心的事"}],
                "ai_emotion": {
                    "mood": "开心",
                    "energy": 75,
                    "dimensions": [
                        {"name": "喜悦", "value": 48},
                        {"name": "好奇", "value": 55},
                    ],
                    "reason": "关系和用户情绪共同带动了 AI 情绪",
                },
            },
            "memory_planner": {"requests": [{
                "memory_type": "semantic", "query": "咖啡偏好", "keywords": ["咖啡"],
                "priority": 4, "top_k": 3, "time_range": "all", "reason": "当前话题相关",
            }]},
            "dialogue_composer": {
                "goal": "自然回应", "strategy": ["简短回应"], "structure": ["一句回应"],
                "memory_usage": [], "persona_expression": ["简洁"], "emotion_expression": [],
                "topic_plan": ["延续当前话题"], "constraints": ["不追问"],
            },
            "prompt_composer": {"candidates": ["好呀", "可以", "听起来不错"]},
            "critic": critique(True),
            "reflection": {
                "memories": [{
                    "category": "semantic",
                    "content": "用户喜欢手冲咖啡",
                    "sensitive": False,
                    "occurred_at": "2026-08-01T09:30:00+00:00",
                }],
                "habit_updates": [], "core_update_proposals": [],
            },
        }

    def invoke(self, responses):
        counts = {}
        lock = threading.Lock()

        def agent_factory(name, _provider="default"):
            return FakeAgent(name, responses, counts, lock)

        with patch("src.graph.store", self.store), patch("src.graph.get_agent", side_effect=agent_factory):
            result = build_graph().invoke({
                "contact_id": self.contact_id,
                "user_request": "今天喝了手冲咖啡",
                "image_data_urls": [],
                "warnings": [],
            })
        return result, counts

    def test_full_pipeline_persists_reflection_and_relationship(self):
        result, counts = self.invoke(self.base_responses())
        self.assertEqual(result["candidates"], ["好呀", "可以", "听起来不错"])
        self.assertEqual(counts["critic"], 1)
        self.assertEqual(self.store.get_relationship(self.contact_id)["familiarity"], 12)
        self.assertEqual(self.store.get_ai_emotion_state(self.contact_id)["mood"], "开心")
        self.assertEqual(self.store.list_memories(self.contact_id)[0]["category"], "semantic")
        self.assertEqual(self.store.list_memories(self.contact_id)[0]["occurred_at"], "2026-08-01T09:30:00+00:00")
        self.assertGreater(result["generation_id"], 0)

    def test_critic_allows_two_rewrites_and_returns_best_candidates(self):
        responses = self.base_responses()
        responses["prompt_composer"] = [
            {"candidates": ["初稿一", "初稿二", "初稿三"]},
            {"candidates": ["二稿一", "二稿二", "二稿三"]},
            {"candidates": ["三稿一", "三稿二", "三稿三"]},
        ]
        responses["critic"] = [
            critique(False, naturalness=9, instruction="第一次修改"),
            critique(False, naturalness=6, instruction="第二次修改"),
            critique(False, naturalness=5, instruction="仍未通过"),
        ]
        result, counts = self.invoke(responses)
        self.assertEqual(counts["prompt_composer"], 3)
        self.assertEqual(counts["critic"], 3)
        self.assertEqual(result["candidates"], ["初稿一", "初稿二", "初稿三"])
        self.assertTrue(any("最佳候选" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()