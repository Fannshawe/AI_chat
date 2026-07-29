from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.storage import ChatStore


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = ChatStore(root / "chat.db", root / "uploads")
        self.store.initialize()
        self.contact_id = self.store.list_contacts()[0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_real_message_round_trip(self):
        saved = self.store.save_message(self.contact_id, "user", "喜欢猫", ["/uploads/a.jpg"])
        messages = self.store.list_messages(self.contact_id)
        self.assertEqual(saved["content"], "喜欢猫")
        self.assertEqual(messages[0]["image_paths"], ["/uploads/a.jpg"])

    def test_memory_deduplication_and_search(self):
        first = self.store.add_memory(self.contact_id, "人物事实与偏好", "对方喜欢猫")
        second = self.store.add_memory(self.contact_id, "人物事实与偏好", "对方喜欢猫")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.search_memories(self.contact_id, "猫")[0]["content"], "对方喜欢猫")

    def test_relationship_requires_manual_confirmation(self):
        suggested = self.store.suggest_relationship(self.contact_id, "朋友", "互动稳定")
        self.assertEqual(suggested["current_level"], "认识")
        self.assertEqual(suggested["suggested_level"], "朋友")
        confirmed = self.store.set_relationship(self.contact_id, "朋友")
        self.assertEqual(confirmed["current_level"], "朋友")
        self.assertIsNone(confirmed["suggested_level"])


if __name__ == "__main__":
    unittest.main()
