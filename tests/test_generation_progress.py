import unittest
from unittest.mock import patch

import app


class GenerationProgressTests(unittest.TestCase):
    def test_response_composer_progress_is_reported_when_task_starts(self):
        events = [
            ("tasks", {"name": "response_composer", "input": {}}),
            ("values", {"candidates": []}),
        ]
        graph = type("FakeGraph", (), {
            "stream": lambda self, *_args, **_kwargs: iter(events),
        })()
        progress_updates = []

        with patch.object(app, "chat_graph", graph), patch.object(
            app, "_debug_settings", return_value={"enabled": False},
        ):
            app._execute_generation(
                1,
                "hello",
                progress_callback=lambda stage, progress: progress_updates.append((stage, progress)),
            )

        self.assertEqual(
            progress_updates,
            [("读取上下文", 6), ("生成候选", 85)],
        )

    def test_execute_generation_returns_formal_response_plan(self):
        events = [
            (
                "values",
                {
                    "strategy": "回应当前消息并自然延续对话",
                    "candidates": ["一", "二", "三"],
                    "response_composition": {
                        "goal": "回应当前消息并自然延续对话",
                        "strategy": ["先接住对方情绪"],
                        "structure": ["先回应，再轻轻推进"],
                        "candidate_directives": [
                            {"angle": "情绪回应"},
                            {"angle": "极简附和"},
                            {"angle": "轻反问"},
                        ],
                    },
                    "critic_analysis": {},
                    "relationship": {"current_level": "认识"},
                    "warnings": [],
                },
            ),
        ]
        graph = type("FakeGraph", (), {
            "stream": lambda self, *_args, **_kwargs: iter(events),
        })()

        with patch.object(app, "chat_graph", graph), patch.object(
            app, "_debug_settings", return_value={"enabled": False},
        ):
            result = app._execute_generation(1, "hello", progress_callback=lambda *_args: None)

        self.assertEqual(result["response_plan"]["goal"], "回应当前消息并自然延续对话")
        self.assertEqual(result["dialogue_plan"], result["response_plan"])
        self.assertEqual(result["candidate_modes"], ["情绪回应", "极简附和", "轻反问"])


if __name__ == "__main__":
    unittest.main()