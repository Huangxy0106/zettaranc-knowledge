from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from live_sentinel.audio.route import MacOSAudioOutputRoute


class MacOSAudioOutputRouteTests(unittest.TestCase):
    @staticmethod
    def _run_for(outputs: list[str]):
        switch_outputs = iter(outputs)

        def run(command, *_args, **_kwargs):
            if command[0] == "SwitchAudioSource":
                return subprocess.CompletedProcess(
                    command, 0, stdout=next(switch_outputs), stderr=""
                )
            if command[:2] == ["osascript", "-e"] and command[2].startswith(
                "set volume"
            ):
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if command[:2] == ["osascript", "-e"]:
                return subprocess.CompletedProcess(command, 0, stdout="100\n", stderr="")
            raise AssertionError(f"unexpected command: {command}")

        return run

    def test_switches_to_capture_device_without_restoring_previous_output(self) -> None:
        run = self._run_for(
            [
                "Mac mini扬声器\n",
                "BlackHole 2ch\nMac mini扬声器\n",
                "",
                "BlackHole 2ch\n",
                "BlackHole 2ch\n",
            ]
        )

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
        self.assertIn(
            ["osascript", "-e", "set volume output volume 100 without output muted"],
            commands,
        )

    def test_existing_target_output_gets_full_volume_without_route_change(self) -> None:
        run = self._run_for(
            [
                "BlackHole 2ch\n",
                "BlackHole 2ch\nMac mini扬声器\n",
                "BlackHole 2ch\n",
            ]
        )

        with patch("live_sentinel.audio.route.shutil.which", return_value="/bin/tool"), patch(
            "live_sentinel.audio.route.subprocess.run", side_effect=run
        ) as invoked:
            route = MacOSAudioOutputRoute("BlackHole 2ch")
            route.activate()
        commands = [call.args[0] for call in invoked.call_args_list]
        self.assertNotIn(
            ["SwitchAudioSource", "-s", "BlackHole 2ch", "-t", "output"],
            commands,
        )
        self.assertIn(
            ["osascript", "-e", "set volume output volume 100 without output muted"],
            commands,
        )

    def test_non_blackhole_output_never_changes_volume(self) -> None:
        outputs = iter(["USB Audio\n", "USB Audio\n"])

        def run(command, *_args, **_kwargs):
            return subprocess.CompletedProcess(command, 0, stdout=next(outputs), stderr="")

        with patch("live_sentinel.audio.route.shutil.which", return_value="/bin/tool"), patch(
            "live_sentinel.audio.route.subprocess.run", side_effect=run
        ) as invoked:
            MacOSAudioOutputRoute("USB Audio").activate()
        self.assertEqual(invoked.call_count, 2)
        self.assertTrue(
            all(call.args[0][0] == "SwitchAudioSource" for call in invoked.call_args_list)
        )

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
