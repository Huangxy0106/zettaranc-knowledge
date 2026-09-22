from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from live_sentinel.asr.apple_speech import AppleSpeechCLIStreamingASR
from live_sentinel.models import SpeechSegment


class AppleSpeechAdapterTests(unittest.TestCase):
    def test_returns_primary_timeline_for_shadow_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            command = Path(temp) / "apple-speech"
            command.touch()
            command.chmod(0o700)
            payload = {
                "available": True,
                "installed": True,
                "segments": [
                    {"start_ms": 0, "end_ms": 500, "text": "你好", "final": True},
                    {"start_ms": 500, "end_ms": 1000, "text": "世界", "final": True},
                ],
            }
            completed = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(payload, ensure_ascii=False), stderr=""
            )
            adapter = AppleSpeechCLIStreamingASR(command)
            speech = SpeechSegment(
                start_ms=2_000,
                end_ms=3_000,
                pcm=b"\x01\x00" * 16_000,
                sample_rate=16_000,
                channels=1,
                sample_width=2,
            )
            with patch(
                "live_sentinel.asr.apple_speech.subprocess.run",
                return_value=completed,
            ) as invoked:
                result = adapter.transcribe(speech)
        assert result is not None
        self.assertEqual((result.start_ms, result.end_ms), (2_000, 3_000))
        self.assertEqual(result.text, "你好世界")
        self.assertIn("--locale=zh-CN", invoked.call_args.args[0])

    def test_surfaces_helper_error_without_exposing_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            command = Path(temp) / "apple-speech"
            command.touch()
            command.chmod(0o700)
            completed = subprocess.CompletedProcess(
                [], 1, stdout="", stderr='{"error":"unsupported"}'
            )
            adapter = AppleSpeechCLIStreamingASR(command)
            speech = SpeechSegment(0, 100, b"\0\0" * 1600, 16_000, 1, 2)
            with patch(
                "live_sentinel.asr.apple_speech.subprocess.run",
                return_value=completed,
            ):
                with self.assertRaisesRegex(RuntimeError, "unsupported"):
                    adapter.transcribe(speech)


if __name__ == "__main__":
    unittest.main()
