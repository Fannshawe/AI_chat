import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException

from app import (
    ImportMessage, ImportRequest, MemoryImportRequest, _parse_import_text, app,
    import_ai_memory, import_chat, import_contact_memory,
)
from src.config import BASE_DIR, DATA_DIR, PERSONA_PATH, STATIC_DIR
from src.storage import ChatStore


class ImportTests(unittest.TestCase):
    def test_chat_import_extracts_memories_in_batches(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ChatStore(root / "chat.db")
            store.persona_path = root / "persona.yaml"
            store.initialize()
            contact_id = store.list_contacts()[0]["id"]
            store.set_setting("active_model_provider", "test-provider")
            payloads = []

            class ReflectionAgent:
                def invoke(self, request):
                    payloads.append(json.loads(request["messages"][0]["content"]))
                    return {"structured_response": {"memories": [
                        {"category": "semantic", "content": " durable fact ", "sensitive": True},
                        {"category": "unknown", "content": "another fact"},
                        {"category": "semantic", "content": " "},
                    ]}}

            messages = [ImportMessage(
                role="user" if index % 2 == 0 else "assistant",
                content=f"message {index}", created_at="2025-01-01T12:00:00+00:00",
            ) for index in range(15)]
            messages.extend([ImportMessage(role="system", content="ignore"), ImportMessage(role="user", content=" ")])
            with patch("app.store", store), patch("src.graph.get_agent", return_value=ReflectionAgent()) as agent:
                result = import_chat(ImportRequest(contact_id=contact_id, messages=messages))
                self.assertEqual(result, {"imported": 15, "memories_added": 2, "warnings": []})
                self.assertEqual(agent.call_count, 3)
                agent.assert_called_with("reflection", "test-provider")
                history = [message for payload in payloads for message in payload["recent_conversation_with_time"]]
                self.assertEqual({message["content"] for message in history}, {f"message {index}" for index in range(15)})
                self.assertTrue(all(len(payload["recent_conversation_with_time"]) <= 8 for payload in payloads))
                self.assertEqual(history[0]["created_at"], messages[0].created_at)
                self.assertEqual(history[1]["role"], "assistant")
                self.assertTrue(payloads[1]["existing_relevant_memories"])
                memories = store.list_memories(contact_id)
                self.assertEqual(len(memories), 2)
                self.assertTrue(all(memory["category"] == "semantic" for memory in memories))
                self.assertEqual(next(memory for memory in memories if memory["content"] == "durable fact")["sensitive"], 1)
                source_id = next(message["id"] for message in store.list_messages(contact_id) if message["content"] == "message 4")
                self.assertTrue(all(memory["source_message_id"] == source_id for memory in memories))

    def test_chat_import_reports_reflection_failure_and_skips_empty_input(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ChatStore(root / "chat.db")
            store.persona_path = root / "persona.yaml"
            store.initialize()
            contact_id = store.list_contacts()[0]["id"]
            with patch("app.store", store), patch("src.graph.get_agent", side_effect=RuntimeError("model unavailable")) as agent:
                result = import_chat(ImportRequest(contact_id=contact_id, text="对方: 今天累了"))
                self.assertEqual(result["imported"], 1)
                self.assertEqual(result["memories_added"], 0)
                self.assertIn("model unavailable", result["warnings"][0])
                self.assertEqual(len(store.list_messages(contact_id)), 1)
                self.assertEqual(store.list_memories(contact_id), [])
                agent.reset_mock()
                self.assertEqual(import_chat(ImportRequest(contact_id=contact_id, text="未知: ignore")),
                                 {"imported": 0, "memories_added": 0, "warnings": []})
                import_chat(ImportRequest(contact_id=contact_id, text="我: hello"))
                agent.assert_not_called()
                with self.assertRaises(HTTPException) as error:
                    import_chat(ImportRequest(contact_id=999, text="对方: hello"))
                self.assertEqual(error.exception.status_code, 404)

    def test_supported_speaker_labels(self):
        messages = _parse_import_text("对方: 今天有点累\n我：早点休息呀\n未知: 忽略")
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertEqual(messages[0].content, "今天有点累")

    def test_memory_import_round_trip_and_deduplication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ChatStore(root / "chat.db")
            store.persona_path = root / "persona.yaml"
            store.initialize()
            contact_id = store.list_contacts()[0]["id"]
            with patch("app.store", store):
                store.save_ai_memory_profile({"semantic": ["已有记忆"]})
                request = MemoryImportRequest(text='semantic:\n  - 已有记忆\n  - content: 新记忆\n    source: 手工资料\nshared_experience:\n  - 一起看海')
                self.assertEqual(import_ai_memory(request), {"imported": 2, "skipped": 1})
                self.assertEqual(import_ai_memory(request), {"imported": 0, "skipped": 3})
                self.assertEqual(store.get_ai_memory_profile()["semantic"][1]["source"], "手工资料")
                request = MemoryImportRequest(text='[{"category":"semantic","content":"喜欢猫","sensitive":1,"contact_id":999}]')
                self.assertEqual(import_contact_memory(contact_id, request)["imported"], 1)
                self.assertEqual(import_contact_memory(contact_id, request)["skipped"], 1)
                memory = store.list_memories(contact_id)[0]
                self.assertEqual(memory["owner_id"], contact_id)
                self.assertEqual(memory["sensitive"], 1)

    def test_invalid_memory_import_does_not_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ChatStore(root / "chat.db")
            store.persona_path = root / "persona.yaml"
            store.initialize()
            contact_id = store.list_contacts()[0]["id"]
            with patch("app.store", store):
                for text in ('semantic: [有效, ""]', 'semantic: [', 'unknown: [内容]', 'semantic: 内容', '[]'):
                    with self.subTest(text=text), self.assertRaises(HTTPException):
                        import_ai_memory(MemoryImportRequest(text=text))
                self.assertEqual(store.get_ai_memory_profile()["semantic"], [])
                for text in ('semantic: [有效, ""]', 'shared_experience: [内容]', '[{category: semantic, content: 内容, sensitive: "false"}]', '[{category: semantic, content: A, contact_id: 1}, {category: semantic, content: B, contact_id: 2}]'):
                    with self.subTest(text=text), self.assertRaises(HTTPException):
                        import_contact_memory(contact_id, MemoryImportRequest(text=text))
                self.assertEqual(store.list_memories(contact_id), [])
                with self.assertRaises(HTTPException) as error:
                    import_contact_memory(999, MemoryImportRequest(text='semantic: [内容]'))
                self.assertEqual(error.exception.status_code, 404)

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
