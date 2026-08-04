from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from src.storage import ChatStore


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = ChatStore(root / "chat.db", root / "uploads")
        self.store.persona_path = root / "data" / "persona.yaml"
        self.store.persona_path.parent.mkdir(parents=True, exist_ok=True)
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
        first = self.store.add_memory(self.contact_id, "semantic", "对方喜欢猫")
        second = self.store.add_memory(self.contact_id, "semantic", "对方喜欢猫")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.search_memories(self.contact_id, "猫")[0]["content"], "对方喜欢猫")

    def test_memory_occurrence_time_round_trip_and_time_filter(self):
        recent = self.store.add_memory(
            self.contact_id,
            "episodic",
            "今天一起吃了晚饭",
            occurred_at="2026-08-01T10:00:00+00:00",
        )
        older = self.store.add_memory(
            self.contact_id,
            "episodic",
            "上个月一起逛街",
            occurred_at="2026-06-01T10:00:00+00:00",
        )
        self.assertEqual(recent["occurred_at"], "2026-08-01T10:00:00+00:00")
        listed = self.store.list_memories(self.contact_id)
        self.assertEqual(listed[0]["occurred_at"], "2026-06-01T10:00:00+00:00")
        filtered = self.store.search_memories(self.contact_id, "一起", time_range="40d")
        self.assertEqual([item["id"] for item in filtered], [recent["id"]])

    def test_relationship_requires_manual_confirmation(self):
        suggested = self.store.suggest_relationship(self.contact_id, "朋友", "互动稳定")
        self.assertEqual(suggested["current_level"], "熟人")
        self.assertEqual(suggested["suggested_level"], "朋友")
        confirmed = self.store.set_relationship(self.contact_id, "朋友")
        self.assertEqual(confirmed["current_level"], "朋友")
        self.assertIsNone(confirmed["suggested_level"])

    def test_relationship_scores_are_bounded_and_changes_are_recorded(self):
        changed = self.store.apply_relationship_analysis(self.contact_id, {
            "changes": [{"dimension": "trust", "delta": 20, "reason": "兑现了承诺"}],
            "suggested_level": "朋友",
            "stage_reason": "互动持续稳定",
            "serious_event": False,
        })
        self.assertEqual(changed["current_level"], "朋友")
        self.assertEqual(changed["trust"], 30)
        event = self.store.list_relationship_events(self.contact_id)[0]
        self.assertEqual(event["delta"], 10)
        self.assertEqual(event["reason"], "兑现了承诺")

        unchanged = self.store.apply_relationship_analysis(self.contact_id, {
            "changes": [],
            "suggested_level": "熟人",
            "stage_reason": "普通分歧不足以降级",
            "serious_event": False,
        })
        self.assertEqual(unchanged["current_level"], "朋友")
        downgraded = self.store.apply_relationship_analysis(self.contact_id, {
            "changes": [{"dimension": "trust", "delta": -8, "reason": "严重欺骗"}],
            "suggested_level": "熟人",
            "stage_reason": "发生严重信任破坏",
            "serious_event": True,
        })
        self.assertEqual(downgraded["current_level"], "熟人")

    def test_memory_plan_filters_categories(self):
        self.store.add_memory(self.contact_id, "semantic", "对方喜欢人工智能")
        self.store.add_memory(self.contact_id, "episodic", "昨天一起讨论了项目")
        results = self.store.search_memories_by_plan(self.contact_id, [{
            "memory_type": "semantic", "query": "人工智能", "keywords": ["AI"],
            "priority": 5, "top_k": 3, "time_range": "all",
        }])
        self.assertEqual([item["category"] for item in results], ["semantic"])

    def test_ai_emotion_state_and_events_round_trip(self):
        saved = self.store.save_ai_emotion_state(self.contact_id, {
            "mood": "专注",
            "energy": 42,
            "dimensions": [{"name": "好奇", "value": 66}, {"name": "失落", "value": 12}],
        })
        self.assertEqual(saved["mood"], "专注")
        self.assertEqual(saved["dimensions"]["好奇"], 66)

        self.store.add_conversation_events(self.contact_id, [
            {"event": "好消息", "impact": 0.8, "reason": "对方分享升职"},
        ])
        event = self.store.list_conversation_events(self.contact_id)[0]
        self.assertEqual(event["event"], "好消息")

    def test_selected_candidate_index_is_recorded(self):
        generation_id = self.store.save_generation(self.contact_id, None, "简短回应", ["一", "二", "三"], {})
        self.store.select_generation_candidate(generation_id, self.contact_id, 1)
        generations = self.store.export_all()["generations"]
        self.assertEqual(generations[0]["selected_candidate_index"], 1)
        self.assertEqual(generations[0]["candidates"], ["一", "二", "三"])

    def test_export_yaml_files_splits_data_for_debugging(self):
        self.store.save_message(self.contact_id, "user", "你好", ["/uploads/demo.png"])
        self.store.add_memory(self.contact_id, "semantic", "对方喜欢 YAML")
        self.store.save_ai_emotion_state(self.contact_id, {
            "mood": "开心",
            "energy": 77,
            "dimensions": {"好奇": 70},
        })

        exported = self.store.export_yaml_files()
        export_dir = Path(exported["export_dir"])

        self.assertTrue((export_dir / "00_overview.yaml").exists())
        self.assertTrue((export_dir / "03_messages.yaml").exists())
        self.assertTrue((export_dir / "05_ai_memory.yaml").exists())
        self.assertTrue((export_dir / "06_emotions.yaml").exists())

        messages = yaml.safe_load((export_dir / "03_messages.yaml").read_text(encoding="utf-8"))
        self.assertEqual(messages[0]["image_paths"], ["/uploads/demo.png"])

        emotions = yaml.safe_load((export_dir / "06_emotions.yaml").read_text(encoding="utf-8"))
        self.assertEqual(emotions["ai_emotion_states"][0]["dimensions"]["好奇"], 70)

    def test_write_operations_auto_refresh_yaml_snapshot(self):
        export_dir = self.store.export_dir
        before = yaml.safe_load((export_dir / "03_messages.yaml").read_text(encoding="utf-8"))

        self.store.save_message(self.contact_id, "user", "自动刷新测试")

        messages = yaml.safe_load((export_dir / "03_messages.yaml").read_text(encoding="utf-8"))
        self.assertEqual(len(messages), len(before) + 1)
        self.assertEqual(messages[0]["content"], "自动刷新测试")

    def test_message_write_does_not_rewrite_unrelated_memory_snapshot(self):
        export_dir = self.store.export_dir
        memories_before = (export_dir / "04_contact_memories.yaml").read_text(encoding="utf-8")

        self.store.save_message(self.contact_id, "user", "只影响消息快照")

        memories_after = (export_dir / "04_contact_memories.yaml").read_text(encoding="utf-8")
        messages = yaml.safe_load((export_dir / "03_messages.yaml").read_text(encoding="utf-8"))
        self.assertEqual(memories_before, memories_after)
        self.assertEqual(messages[0]["content"], "只影响消息快照")

    def test_reflection_updates_only_habits_and_queues_core(self):
        self.store.persona_path.write_text(
            "core:\n  name: Lin\npersonality:\n  values: [honesty]\nhabits:\n  likes: []\n",
            encoding="utf-8",
        )
        profile = self.store.update_persona_habits([
            {"key": "favorite_drink", "value": "coffee", "reason": "多次提及"},
        ])
        self.assertEqual(profile["habits"]["favorite_drink"], "coffee")
        self.assertEqual(profile["core"]["name"], "Lin")

        self.store.add_core_update_proposals(self.contact_id, [
            {"key": "city", "value": "上海", "reason": "用户明确修正"},
        ])
        proposals = self.store.list_core_update_proposals(self.contact_id)
        self.assertEqual(proposals[0]["field_key"], "city")
        resolved = self.store.resolve_core_update_proposal(proposals[0]["id"], approved=True)
        self.assertEqual(resolved["status"], "approved")
        self.assertEqual(self.store.get_persona_profile()["core"]["city"], "上海")


if __name__ == "__main__":
    unittest.main()
