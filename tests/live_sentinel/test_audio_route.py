from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from live_sentinel.audio.route import MacOSAudioOutputRoute


class MacOSAudioOutputRouteTests(unittest.TestCase):
    def test_switches_to_capture_device_without_restoring_previous_output(self) -> None:
        outputs = iter(
            [
                "Mac mini扬声器\n",
                "BlackHole 2ch\nMac mini扬声器\n",
                "",
                "BlackHole 2ch\n",
            ]
        )

        def run(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, stdout=next(outputs), stderr="")

        with patch("live_sentinel.audio.route.shutil.which", return_value="/bin/tool"), patch(
            "live_sentinel.audio.route.subprocess.run", side_effect=run
        ) as invoked:
            route = MacOSAudioOutputRoute("BlackHole 2ch")
            self.assertEqual(route.activate(), "Mac mini扬声器")

        commands = [call.args[0] for call in invoked.call_args_list]
        self.assertIn(
            ["SwitchAudioSource", "-s", "BlackHole 2ch", "-t", "output"],
            commands,
        )
        self.assertNotIn(
            ["SwitchAudioSource", "-s", "Mac mini扬声器", "-t", "output"], commands
        )

    def test_existing_target_output_is_not_changed(self) -> None:
        outputs = iter(["BlackHole 2ch\n", "BlackHole 2ch\nMac mini扬声器\n"])

        def run(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, stdout=next(outputs), stderr="")

        with patch("live_sentinel.audio.route.shutil.which", return_value="/bin/tool"), patch(
            "live_sentinel.audio.route.subprocess.run", side_effect=run
        ) as invoked:
            route = MacOSAudioOutputRoute("BlackHole 2ch")
            route.activate()
        self.assertEqual(invoked.call_count, 2)

    def test_missing_target_device_fails_closed(self) -> None:
        outputs = iter(["Mac mini扬声器\n", "Mac mini扬声器\n"])

        def run(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, stdout=next(outputs), stderr="")

        with patch("live_sentinel.audio.route.shutil.which", return_value="/bin/tool"), patch(
            "live_sentinel.audio.route.subprocess.run", side_effect=run
        ):
            route = MacOSAudioOutputRoute("BlackHole 2ch")
            with self.assertRaisesRegex(RuntimeError, "找不到系统音频输出设备"):
                route.activate()


if __name__ == "__main__":
    unittest.main()
