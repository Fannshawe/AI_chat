import unittest

from app import _parse_import_text


class ImportTests(unittest.TestCase):
    def test_supported_speaker_labels(self):
        messages = _parse_import_text("对方: 今天有点累\n我：早点休息呀\n未知: 忽略")
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertEqual(messages[0].content, "今天有点累")


if __name__ == "__main__":
    unittest.main()
