from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from live_sentinel.launchd import DEFAULT_SERVICE_PATH, write_plist


class LaunchdPlistTests(unittest.TestCase):
    def test_service_path_includes_homebrew_and_system_bins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "watchlist.plist"
            with patch("live_sentinel.launchd.plist_path", return_value=destination):
                write_plist(
                    config_path=None,
                    env_file=None,
                    state_db=None,
                    working_directory=tmp,
                )

            payload = plistlib.loads(destination.read_bytes())
            self.assertEqual(payload["EnvironmentVariables"]["PATH"], DEFAULT_SERVICE_PATH)
            self.assertIn("/opt/homebrew/bin", DEFAULT_SERVICE_PATH.split(":"))
            self.assertIn("/usr/bin", DEFAULT_SERVICE_PATH.split(":"))
            self.assertEqual(payload["ProcessType"], "Interactive")
            self.assertTrue(payload["StandardErrorPath"].endswith("watchlist.launchd.err.log"))


if __name__ == "__main__":
    unittest.main()
