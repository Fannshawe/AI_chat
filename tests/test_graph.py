from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from src.graph import _normalize_segments, _prompt_composer_payload, _rewrite_payload, build_graph, node_dialogue_composer, node_persona_habit_retrieval
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
            "persona_consistent": True,
            "memory_consistent": True,
            "passed": passed,
            "issues": [] if passed else ["需要更自然"],
            "rewrite_instruction": instruction if not passed else "",
        } for index in range(3)],
        "passed": passed,
        "rewrite_candidate_indexes": [] if passed else [0, 1, 2],
    }


def partial_critique(failed_indexes, instruction_prefix="需要修改"):
    failed = set(failed_indexes)
    return {
        "candidates": [{
            "candidate_index": index,
            "ai_taste": 6 if index in failed else 1,
            "interview_taste": 6 if index in failed else 1,
            "empathy_excess": 6 if index in failed else 1,
            "naturalness": 4 if index in failed else 8,
            "persona_consistent": True,
            "memory_consistent": True,
            "passed": index not in failed,
            "issues": [f"{index} 需要更自然"] if index in failed else [],
            "rewrite_instruction": f"{instruction_prefix}{index}" if index in failed else "",
        } for index in range(3)],
        "passed": not failed,
        "rewrite_candidate_indexes": sorted(failed),
    }


class GraphPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = ChatStore(root / "chat.db")
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

    def test_expression_style_is_passed_to_composer_and_rewrite_nodes(self):
        expression_style = {"segmentation": {"typical_segments": "1-2"}}
        state = {"user_request": "hello", "expression_style": expression_style}
        with patch("src.graph._run_structured_agent", return_value=({}, [], None)) as run_agent:
            node_dialogue_composer(state)
        dialogue_payload = run_agent.call_args.args[2]
        for payload in (dialogue_payload, _prompt_composer_payload(state), _rewrite_payload(state)):
            self.assertEqual(payload["persona"]["expression_style"], expression_style)

    def base_responses(self):
        return {
            "emotion": {"emotions": [{
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
            "dialogue_composer": {
                "goal": "自然回应", "strategy": ["简短回应"], "structure": ["一句回应"],
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
            },
            "prompt_composer": {"candidates": ["好呀", "可以", "听起来不错"]},
            "rewrite": {"candidates": ["改好的一", "改好的二", "改好的三"]},
            "critic": critique(True),
            "reflection": {
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

    def test_full_pipeline_persists_reflection_and_relationship(self):
        result, counts = self.invoke(self.base_responses())
        self.assertEqual(result["candidates"], ["好呀", "可以", "听起来不错"])
        self.assertEqual(counts["critic"], 1)
        self.assertEqual(self.store.get_relationship(self.contact_id)["familiarity"], 12)
        self.assertEqual(self.store.get_self_emotion_state(self.contact_id)["mood"], "开心")
        self.assertEqual(self.store.list_memories(self.contact_id)[0]["category"], "semantic")
        self.assertIn("created_at", self.store.list_memories(self.contact_id)[0])
        self.assertGreater(result["generation_id"], 0)

    def test_critic_allows_two_rewrites_and_returns_fallback_candidates(self):
        responses = self.base_responses()
        responses["prompt_composer"] = [
            {"candidates": ["初稿一", "初稿二", "初稿三"]},
        ]
        responses["rewrite"] = [
            {"candidates": ["二稿一", "二稿二", "二稿三"]},
            {"candidates": ["三稿一", "三稿二", "三稿三"]},
        ]
        responses["critic"] = [
            critique(False, naturalness=9, instruction="第一次修改"),
            critique(False, naturalness=6, instruction="第二次修改"),
            critique(False, naturalness=5, instruction="仍未通过"),
        ]
        result, counts = self.invoke(responses)
        self.assertEqual(counts["prompt_composer"], 1)
        self.assertEqual(counts["rewrite"], 2)
        self.assertEqual(counts["critic"], 3)
        self.assertEqual(result["candidates"], ["初稿一", "初稿二", "初稿三"])
        self.assertTrue(any("最佳候选" in warning for warning in result["warnings"]))

    def test_critic_marks_only_failed_candidates_for_rewrite(self):
        responses = self.base_responses()
        responses["critic"] = partial_critique([1])
        result, _counts = self.invoke(responses)
        self.assertEqual(result["critic_analysis"]["rewrite_candidate_indexes"], [1])
        self.assertIn("需要修改1", result["critic_analysis"]["rewrite_instruction"])

    def test_rewrite_only_replaces_failed_candidates(self):
        responses = self.base_responses()
        responses["prompt_composer"] = {"candidates": ["原候选一", "原候选二", "原候选三"]}
        responses["critic"] = [
            partial_critique([1]),
            critique(True),
        ]
        responses["rewrite"] = [
            {"candidates": [{"candidate_index": 1, "content": "只改第二条"}]},
        ]
        result, counts = self.invoke(responses)
        self.assertEqual(counts["prompt_composer"], 1)
        self.assertEqual(counts["rewrite"], 1)
        self.assertEqual(result["candidates"], ["原候选一", "只改第二条", "原候选三"])
        self.assertEqual(result["fallback_candidates"], ["原候选一", "只改第二条", "原候选三"])

    def test_rewrite_ignores_unrequested_candidate_indexes(self):
        responses = self.base_responses()
        responses["prompt_composer"] = {"candidates": ["原候选一", "原候选二", "原候选三"]}
        responses["critic"] = [
            partial_critique([1]),
            critique(True),
        ]
        responses["rewrite"] = [{"candidates": [
            {"candidate_index": 0, "content": "不应替换"},
            {"candidate_index": 1, "content": "只改第二条"},
        ]}]
        result, _counts = self.invoke(responses)
        self.assertEqual(result["candidates"], ["原候选一", "只改第二条", "原候选三"])

    def test_rewrite_payload_contains_only_requested_candidates(self):
        payload = _rewrite_payload({
            "candidates": ["原候选一", "原候选二", "原候选三"],
            "critic_analysis": {
                "rewrite_instruction": "只改第二条",
                "rewrite_candidate_indexes": [1],
            },
            "dialogue_composition": {"goal": "自然回应"},
        })
        self.assertEqual(
            payload["rewrite_targets"],
            [{"candidate_index": 1, "existing_candidate": "原候选二"}],
        )
        self.assertNotIn("existing_candidates", payload)

    def test_prompt_composer_normalizes_segment_line_breaks(self):
        responses = self.base_responses()
        responses["prompt_composer"] = {"candidates": [
            "今天去公司了。\n\n结果老板不在😂\n\n下午就提前回来了。",
            "哈哈哈",
            "一\n二\n三\n四\n五\n六",
        ]}
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