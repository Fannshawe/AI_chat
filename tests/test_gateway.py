import logging
import unittest
from unittest.mock import patch

import gateway


class GatewayRestartTests(unittest.TestCase):
    def test_restart_endpoint_restarts_current_session_user(self):
        record = object()

        with patch("gateway._current_username", return_value="alice"), patch.object(
            gateway.instance_manager,
            "restart_instance",
            return_value=type("Record", (), {"username": "alice", "port": 8101})(),
        ) as restart:
            result = gateway.gateway_restart_current_instance(object())

        restart.assert_called_once_with("alice")
        self.assertEqual(result, {"ok": True, "username": "alice", "port": 8101})

    def test_job_polling_access_log_filter_hides_only_job_id_requests(self):
        log_filter = gateway._JobPollingAccessLogFilter()
        polling_record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            "",
            ("127.0.0.1:51254", "GET", "/api/chat/generate/jobs/d1fa29eb2c8149149d178dae3c699e29", "1.1", 200),
            None,
        )
        active_record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            "",
            ("127.0.0.1:51254", "GET", "/api/chat/generate/jobs/active", "1.1", 200),
            None,
        )

        self.assertFalse(log_filter.filter(polling_record))
        self.assertTrue(log_filter.filter(active_record))


if __name__ == "__main__":
    unittest.main()