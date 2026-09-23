import os
import subprocess
import unittest
from unittest.mock import patch

from live_sentinel.browser_playback import BrowserPlaybackController
from live_sentinel.bilibili import BilibiliRoomInfo
from live_sentinel.config import AppConfig
from live_sentinel.runner import (
    _bilibili_room_status_probe,
    _browser_audio_url_from_env,
    _browser_capture_required,
    _playback_output_device,
)


class BrowserAudioUrlTests(unittest.TestCase):
    def test_room_probe_preserves_live_state_when_stream_becomes_unavailable(self) -> None:
        fallback = BilibiliRoomInfo(
            room_id=11163068,
            requested_room_id=11163068,
            creator_uid="326246517",
            uname="zettaranc",
            title="paid section",
            live_status=1,
        )
        with patch(
            "live_sentinel.runner.fetch_room_info",
            side_effect=[RuntimeError("protected"), fallback],
        ):
            observed = _bilibili_room_status_probe(11163068, timeout=1)
        self.assertTrue(observed.is_live)
        self.assertEqual(observed.access_mode, "protected_or_unavailable")

    def test_room_policy_forces_browser_audio_before_drm_transition(self) -> None:
        config = AppConfig.from_mapping(
            {"bilibili_capture": {"browser_audio_room_ids": ["11163068"]}}
        )
        info = BilibiliRoomInfo(
            room_id=11163068,
            requested_room_id=1616,
            creator_uid="326246517",
            uname="zettaranc",
            title="public opening",
            live_status=1,
            stream_url="https://example.test/public.flv",
            stream_drm=False,
        )
        self.assertTrue(_browser_capture_required(info, config))

    def test_accepts_ipv4_loopback_url(self) -> None:
        url = "http://127.0.0.1:18766/token/stream"
        with patch.dict(os.environ, {"BILIBILI_DRM_AUDIO_URL": url}, clear=False):
            self.assertEqual(_browser_audio_url_from_env(), url)

    def test_rejects_non_loopback_or_credentialed_urls(self) -> None:
        invalid = [
            "https://127.0.0.1:18766/token/stream",
            "http://localhost:18766/token/stream",
            "http://example.com/stream",
            "http://user:secret@127.0.0.1/stream",
        ]
        for url in invalid:
            with self.subTest(url=url), patch.dict(
                os.environ, {"BILIBILI_DRM_AUDIO_URL": url}, clear=False
            ):
                with self.assertRaisesRegex(RuntimeError, "本机回环"):
                    _browser_audio_url_from_env()

    def test_empty_value_disables_bridge(self) -> None:
        with patch.dict(os.environ, {"BILIBILI_DRM_AUDIO_URL": ""}, clear=False):
            self.assertIsNone(_browser_audio_url_from_env())

    def test_playback_output_can_differ_from_capture_device(self) -> None:
        self.assertEqual(
            _playback_output_device("BlackHole 2ch", "LiveSentinel Monitor"),
            "LiveSentinel Monitor",
        )

    def test_empty_playback_output_preserves_silent_capture_route(self) -> None:
        self.assertEqual(
            _playback_output_device("BlackHole 2ch", ""),
            "BlackHole 2ch",
        )

    def test_playback_output_rejects_multiline_device_name(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "playback_output_device"):
            _playback_output_device("BlackHole 2ch", "Monitor\nInjected")

    def test_browser_controller_opens_only_authorized_bilibili_page(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        controller = BrowserPlaybackController(
            "Microsoft Edge", "https://live.bilibili.com/11163068"
        )
        with patch(
            "live_sentinel.browser_playback.shutil.which",
            side_effect=lambda command: None if command == "osascript" else "/usr/bin/open",
        ), patch(
            "live_sentinel.browser_playback.subprocess.run", return_value=completed
        ) as invoked:
            result = controller.start()
        self.assertEqual(result, "opened_fallback")
        self.assertEqual(
            invoked.call_args.args[0],
            ["open", "-a", "Microsoft Edge", "https://live.bilibili.com/11163068"],
        )

    def test_browser_controller_reuses_short_room_alias_in_edge(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="reused\n", stderr="")
        controller = BrowserPlaybackController(
            "Microsoft Edge",
            "https://live.bilibili.com/11163068",
            room_ids=("11163068", "1616"),
        )
        with patch("live_sentinel.browser_playback.shutil.which", return_value="/usr/bin/osascript"), patch(
            "live_sentinel.browser_playback.subprocess.run", return_value=completed
        ) as invoked:
            result = controller.start()
        self.assertEqual(result, "reused")
        command = invoked.call_args.args[0]
        self.assertEqual(command[0:2], ["osascript", "-e"])
        self.assertEqual(command[-2:], ["11163068", "1616"])

    def test_browser_controller_falls_back_when_edge_automation_fails(self) -> None:
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="not permitted")
        opened = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        controller = BrowserPlaybackController(
            "Microsoft Edge", "https://live.bilibili.com/11163068"
        )
        with patch("live_sentinel.browser_playback.shutil.which", return_value="/usr/bin/tool"), patch(
            "live_sentinel.browser_playback.subprocess.run",
            side_effect=[failed, opened],
        ) as invoked:
            result = controller.start()
        self.assertEqual(result, "opened_fallback")
        self.assertEqual(invoked.call_count, 2)

    def test_browser_controller_rejects_unrelated_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "B 站直播页"):
            BrowserPlaybackController("Microsoft Edge", "https://example.com").start()
