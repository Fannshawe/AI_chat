import unittest

from app import _parse_import_text, app
from src.config import BASE_DIR, DATA_DIR, PERSONA_PATH, STATIC_DIR


class ImportTests(unittest.TestCase):
    def test_supported_speaker_labels(self):
        messages = _parse_import_text("对方: 今天有点累\n我：早点休息呀\n未知: 忽略")
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertEqual(messages[0].content, "今天有点累")

    def test_single_instance_paths_and_pages(self):
        self.assertEqual(DATA_DIR, BASE_DIR / "data")
        self.assertEqual(PERSONA_PATH, DATA_DIR / "persona.yaml")
        paths = {route.path for route in app.routes}
        self.assertTrue({"/", "/admin", "/debug"}.issubset(paths))
        self.assertFalse(any(path.startswith("/gateway") for path in paths))
        for page_name in ("index", "admin", "debug"):
            for extension in ("html", "js"):
                with self.subTest(page=page_name, extension=extension):
                    content = (STATIC_DIR / f"{page_name}.{extension}").read_text(encoding="utf-8")
                    self.assertNotIn("/gateway", content)
                    self.assertNotIn("logoutButton", content)


if __name__ == "__main__":
    unittest.main()
