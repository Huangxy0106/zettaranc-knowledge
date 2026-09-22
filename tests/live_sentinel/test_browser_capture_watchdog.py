import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from live_sentinel.browser_watchdog_launchd import write_one_shot_plist
from scripts.browser_capture_watchdog import (
    terminate_verified_receiver,
    write_report_once,
)


class BrowserCaptureWatchdogTests(unittest.TestCase):
    @patch("scripts.browser_capture_watchdog.subprocess.run")
    @patch("scripts.browser_capture_watchdog.os.kill")
    def test_only_terminates_matching_receiver(self, kill, run) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "python scripts/browser_audio_receiver.py --port 18766"
        self.assertTrue(terminate_verified_receiver(123, 18766))
        kill.assert_called_once_with(123, 15)

    @patch("scripts.browser_capture_watchdog.subprocess.run")
    @patch("scripts.browser_capture_watchdog.os.kill")
    def test_refuses_reused_pid(self, kill, run) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "python unrelated.py"
        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            terminate_verified_receiver(123, 18766)
        kill.assert_not_called()

    def test_terminal_report_is_atomic_and_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchdog.json"
            self.assertTrue(write_report_once(path, {"bridge_stopped": True}))
            self.assertFalse(write_report_once(path, {"bridge_stopped": False}))
            self.assertEqual(json.loads(path.read_text()), {"bridge_stopped": True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_launchd_plist_is_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plist = write_one_shot_plist(
                program_arguments=["/usr/bin/python3", "/tmp/watchdog.py"],
                working_directory=root,
                stdout_path=root / "out.log",
                stderr_path=root / "err.log",
                destination=root / "watchdog.plist",
            )
            payload = plistlib.loads(plist.read_bytes())
            self.assertIs(payload["KeepAlive"], False)
            self.assertIs(payload["RunAtLoad"], True)
            self.assertEqual(
                payload["ProgramArguments"],
                ["/usr/bin/python3", "/tmp/watchdog.py"],
            )
            self.assertEqual(plist.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
