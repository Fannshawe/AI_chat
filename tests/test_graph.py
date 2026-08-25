from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from src.graph import _normalize_segments, build_graph, node_persona_habit_retrieval
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


def critique(passed, failed_indexes=(), revised_candidates=None):
    failed = set(failed_indexes)
    revised_candidates = revised_candidates or {}
    return {
        "reviews": [{
            "candidate_index": index,
            "ai_taste": 6 if index in failed else 1,
            "interview_taste": 1,
            "empathy_excess": 1,
            "naturalness": 4 if index in failed else 8,
            "persona_consistent": True,
            "memory_consistent": True,
            "passed": index not in failed,
            "issues": ["需要更自然"] if index in failed else [],
            "rewrite_instruction": "改得更自然" if index in failed else "",
            "revised_candidate": revised_candidates.get(index, "") if index in failed else "",
        } for index in range(3)],
        "passed": passed,
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
            "turn_interpreter": {
                "contact_emotion": {"emotions": [{
                    "subject": "对方", "emotion": "开心", "intensity": 0.8,
                    "confidence": 0.9, "reason": "用户分享了积极近况",
                }]},
                "relationship": {
                    "changes": [{"dimension": "familiarity", "delta": 2, "reason": "自然分享近况"}],
                    "suggested_level": "认识",
                    "stage_reason": "保持当前阶段",
                    "serious_event": False,
                    "behavior_boundaries": ["保持自然称呼"],
                },
            },
            "self_emotion": {
                "events": [{"event": "好消息", "impact": 0.7, "reason": "用户在分享开心的事"}],
                "self_emotion_state": {
                    "mood": "开心",
                    "mood_intensity": 65,
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
            "response_composer": {
                "goal": "自然回应", "strategy": ["简短回应"],
                "structure": ["一句回应"],
                "memory_usage": [], "persona_expression": ["简洁"], "emotion_expression": [],
                "topic_plan": ["延续当前话题"],
                "length_plan": {
                    "sentence_count": 1, "char_budget": 15,
                    "allow_filler_only": True, "allow_double_text": False, "rhythm_note": "短句",
                },
                "candidate_directives": [
                    {"candidate_index": 0, "angle": "情绪回应", "length_hint": "短", "segment_count": 1, "instruction": "接住状态"},
                    {"candidate_index": 1, "angle": "极简附和", "length_hint": "极短", "segment_count": 1, "instruction": "几个字"},
                    {"candidate_index": 2, "angle": "轻反问", "length_hint": "短", "segment_count": 1, "instruction": "问一句"},
                ],
                "constraints": ["不追问"],
                "candidates": ["好呀", "可以", "听起来不错"],
            },
            "critic": critique(True),
            "user_mem_recognition": {
                "memories": [{
                    "category": "semantic",
                    "content": "用户喜欢手冲咖啡",
                    "sensitive": False,
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
                "warnings": [],
            })
        return result, counts

    def test_full_pipeline_returns_recognized_memory_and_relationship_analysis(self):
        result, counts = self.invoke(self.base_responses())
        self.assertEqual(result["candidates"], ["好呀", "可以", "听起来不错"])
        self.assertEqual(counts["turn_interpreter"], 1)
        self.assertEqual(counts["response_composer"], 1)
        self.assertEqual(counts["critic"], 1)
        self.assertEqual(counts["user_mem_recognition"], 1)
        self.assertEqual(result["relationship_analysis"]["changes"][0]["dimension"], "familiarity")
        self.assertEqual(result["self_emotion_update"]["self_emotion_state"]["mood"], "开心")
        self.assertEqual(result["user_memory_recognition"]["memories"][0]["category"], "semantic")
        trace_nodes = [entry["node"] for entry in result["debug_trace"]]
        self.assertLess(trace_nodes.index("critic_complete"), trace_nodes.index("save"))
        self.assertLess(trace_nodes.index("user_mem_recognition"), trace_nodes.index("save"))

    def test_response_composition_keeps_full_formal_plan_fields(self):
        result, _counts = self.invoke(self.base_responses())
        self.assertEqual(result["response_composition"]["strategy"], ["简短回应"])
        self.assertEqual(result["response_composition"]["memory_usage"], [])
        self.assertEqual(result["response_composition"]["persona_expression"], ["简洁"])
        self.assertEqual(result["response_composition"]["emotion_expression"], [])

    def test_critic_revises_failed_candidate_then_reviews_again(self):
        responses = self.base_responses()
        responses["critic"] = [
            critique(False, failed_indexes=[1], revised_candidates={1: "只改第二条"}),
            critique(True),
        ]
        result, counts = self.invoke(responses)
        self.assertEqual(counts["response_composer"], 1)
        self.assertEqual(counts["critic"], 2)
        self.assertEqual(result["candidates"], ["好呀", "只改第二条", "听起来不错"])
        self.assertEqual(result["critic_analysis"]["revised_candidate_indexes"], [])

    def test_skips_critic_when_max_attempts_is_zero(self):
        responses = self.base_responses()

        with patch("src.graph.CRITIC_MAX_ATTEMPTS", 0):
            result, counts = self.invoke(responses)

        self.assertEqual(counts["response_composer"], 1)
        self.assertNotIn("critic", counts)
        self.assertEqual(counts["user_mem_recognition"], 1)
        self.assertEqual(result["candidates"], ["好呀", "可以", "听起来不错"])

    def test_response_composer_normalizes_segment_line_breaks(self):
        responses = self.base_responses()
        responses["response_composer"]["candidates"] = [
            "今天去公司了。\n\n结果老板不在😂\n\n下午就提前回来了。",
            "哈哈哈",
            "一\n二\n三\n四\n五\n六",
        ]
        result, _counts = self.invoke(responses)
        self.assertEqual(
            result["candidates"][0].split("\n"),
            ["今天去公司了。", "结果老板不在😂", "下午就提前回来了。"],
        )
        self.assertEqual(result["candidates"][1], "哈哈哈")
        self.assertEqual(result["candidates"][2].split("\n"), ["一", "二", "三", "四", "五六"])


class NormalizeSegmentsTests(unittest.TestCase):
    def test_collapses_blank_lines_between_ideas(self):
        text = "今天去公司了。\n\n结果老板不在😂\n\n下午就提前回来了。"
        self.assertEqual(
            _normalize_segments(text), "今天去公司了。\n结果老板不在😂\n下午就提前回来了。",
        )

    def test_keeps_silence_marker_untouched(self):
        self.assertEqual(_normalize_segments("（暂不回复）"), "（暂不回复）")

    def test_caps_segment_count_by_merging_tail(self):
        text = "一\n二\n三\n四\n五\n六"
        self.assertEqual(_normalize_segments(text, max_segments=5), "一\n二\n三\n四\n五六")

    def test_keeps_structured_list_as_single_message(self):
        text = "可以试试：\n- A\n- B\n- C"
        self.assertEqual(_normalize_segments(text), text)

    def test_single_segment_passthrough(self):
        self.assertEqual(_normalize_segments("哈哈哈"), "哈哈哈")


class PersonaHabitRetrievalTests(unittest.TestCase):
    def test_retrieves_favorite_movies_from_movie_question(self):
        result = node_persona_habit_retrieval({
            "user_request": "你喜欢什么电影？",
            "persona_habits": {
                "topics": {
                    "favorite_movies": ["花样年华", "海街日记"],
                    "music": ["独立流行"],
                }
            },
        })
        self.assertEqual(
            result["relevant_habits"].get("topics.favorite_movies"),
            ["花样年华", "海街日记"],
        )


if __name__ == "__main__":
    unittest.main()