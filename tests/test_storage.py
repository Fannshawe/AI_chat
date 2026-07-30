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

    def test_life_state_daily_history_round_trip(self):
        saved = self.store.save_life_state({
            "state_date": "2026-07-29",
            "mood": "有点疲惫",
            "energy": 35,
            "curiosity": 60,
            "attachment": 25,
            "current_activity": "整理项目",
            "current_focus": "摄影",
            "today_events": ["改了聊天程序"],
            "yesterday_events": ["看了展"],
            "extras": {"天气": "下雨"},
        })
        self.assertEqual(saved["today_events"], ["改了聊天程序"])
        self.assertEqual(saved["extras"]["天气"], "下雨")
        self.assertEqual(self.store.list_life_states()[0]["state_date"], "2026-07-29")

    def test_high_confidence_adjacent_relationship_change_is_automatic(self):
        changed = self.store.apply_relationship_analysis(self.contact_id, "朋友", 0.93, "长期稳定互动")
        self.assertEqual(changed["current_level"], "朋友")
        self.assertIsNone(changed["suggested_level"])

        suggested = self.store.apply_relationship_analysis(self.contact_id, "恋人", 0.99, "不能跨阶段")
        self.assertEqual(suggested["current_level"], "朋友")
        self.assertEqual(suggested["suggested_level"], "恋人")

    def test_selected_candidate_index_is_recorded(self):
        generation_id = self.store.save_generation(self.contact_id, None, "简短回应", ["一", "二", "三"], {})
        self.store.select_generation_candidate(generation_id, self.contact_id, 1)
        generations = self.store.export_all()["generations"]
        self.assertEqual(generations[0]["selected_candidate_index"], 1)


if __name__ == "__main__":
    unittest.main()
