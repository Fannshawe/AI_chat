from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.multi_instance import GATEWAY_LOGGER, InstanceManager, UserRegistry, configure_gateway_log_relay, verify_password


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

    def test_restart_stops_then_starts_the_current_instance(self):
        record = self.registry.create_user("alice", "secret123")
        manager = InstanceManager(self.root, self.registry)
        actions: list[str] = []

        with patch.object(manager, "stop_instance", side_effect=lambda username: actions.append(f"stop:{username}")), patch.object(
            manager,
            "ensure_running",
            side_effect=lambda username: actions.append(f"start:{username}") or record,
        ):
            self.assertEqual(manager.restart_instance("alice"), record)

        self.assertEqual(actions, ["stop:alice", "start:alice"])

    def test_instance_process_uses_utf8_standard_output(self):
        record = self.registry.create_user("alice", "secret123")
        manager = InstanceManager(self.root, self.registry)
        captured_environment: dict[str, str] = {}

        with patch("src.multi_instance.subprocess.Popen") as popen, patch("src.multi_instance._port_is_open", return_value=True):
            process = popen.return_value
            process.poll.return_value = None
            process.stdout = None
            manager._start_process(record)
            captured_environment = popen.call_args.kwargs["env"]

        self.assertEqual(captured_environment["PYTHONIOENCODING"], "utf-8")

    def test_debug_output_is_relayed_to_gateway_only(self):
        manager = InstanceManager(self.root, self.registry)
        log_path = self.root / "instance.log"

        with patch("src.multi_instance.GATEWAY_LOGGER.debug") as debug:
            manager._relay_process_output(
                "alice",
                _LineStream("INFO: ignored\n[DEBUG] included\n[workflow-debug] start\ndetails\n[/workflow-debug]\n"),
                log_path,
            )

        self.assertEqual(
            [call.args for call in debug.call_args_list],
            [
                ("instance=%s %s", "alice", "[DEBUG] included"),
                ("instance=%s %s", "alice", "[workflow-debug] start"),
                ("instance=%s %s", "alice", "details"),
                ("instance=%s %s", "alice", "[/workflow-debug]"),
            ],
        )
        self.assertIn("INFO: ignored", log_path.read_text(encoding="utf-8"))

    def test_gateway_debug_relay_installs_a_dedicated_handler(self):
        original_handlers = list(GATEWAY_LOGGER.handlers)
        original_level = GATEWAY_LOGGER.level
        original_propagate = GATEWAY_LOGGER.propagate
        try:
            GATEWAY_LOGGER.handlers.clear()
            configure_gateway_log_relay()
            self.assertEqual(GATEWAY_LOGGER.level, 10)
            self.assertFalse(GATEWAY_LOGGER.propagate)
            self.assertEqual(len(GATEWAY_LOGGER.handlers), 1)
        finally:
            GATEWAY_LOGGER.handlers[:] = original_handlers
            GATEWAY_LOGGER.setLevel(original_level)
            GATEWAY_LOGGER.propagate = original_propagate


class _LineStream:
    def __init__(self, text: str):
        self.lines = iter(text.splitlines(keepends=True))

    def readline(self) -> str:
        return next(self.lines, "")

    def close(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()