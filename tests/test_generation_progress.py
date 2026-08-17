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


if __name__ == "__main__":
    unittest.main()