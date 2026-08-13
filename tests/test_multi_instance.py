from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.multi_instance import UserRegistry, verify_password


class UserRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        legacy = self.root / "data"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "persona.yaml").write_text("core:\n  name: legacy\n", encoding="utf-8")
        self.registry = UserRegistry(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_first_user_inherits_legacy_data_and_fixed_port(self):
        created = self.registry.create_user("alice", "secret123")
        self.assertEqual(created.port, 8101)
        self.assertTrue((created.data_dir / "persona.yaml").exists())
        self.assertIn("legacy", (created.data_dir / "persona.yaml").read_text(encoding="utf-8"))

    def test_password_verification_and_next_port(self):
        self.registry.create_user("alice", "secret123")
        created = self.registry.create_user("bob", "secret456")
        self.assertEqual(created.port, 8102)
        record = self.registry.verify_user("bob", "secret456")
        self.assertIsNotNone(record)
        raw = self.registry._load()["users"]["bob"]["password"]
        self.assertTrue(verify_password("secret456", raw))


if __name__ == "__main__":
    unittest.main()