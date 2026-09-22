from __future__ import annotations

import logging
from pathlib import Path
import tempfile
import unittest

from live_sentinel.observability import configure_service_logging, redact_payload, redact_text


class ObservabilityTests(unittest.TestCase):
    def test_redaction_covers_keys_bearer_tokens_and_urls(self) -> None:
        raw = (
            "api_key=sk-example-secret-12345678 "
            "Authorization: Bearer abcdefghijklmnop "
            "https://example.test/path?access_token=very-secret-value"
        )
        safe = redact_text(raw)
        self.assertNotIn("sk-example", safe)
        self.assertNotIn("abcdefghijklmnop", safe)
        self.assertNotIn("very-secret-value", safe)
        self.assertGreaterEqual(safe.count("[REDACTED]"), 3)
        payload = redact_payload({"nested": [raw]})
        self.assertNotIn("sk-example", str(payload))

    def test_service_log_is_rotated_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlist.err.log"
            configure_service_logging(path, retention_days=3)
            logging.getLogger("test").error("token=sk-example-secret-12345678")
            for handler in logging.getLogger().handlers:
                handler.flush()
            content = path.read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", content)
            self.assertNotIn("sk-example", content)
            handler = logging.getLogger().handlers[0]
            self.assertEqual(getattr(handler, "backupCount", None), 3)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            logging.getLogger().removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    unittest.main()
