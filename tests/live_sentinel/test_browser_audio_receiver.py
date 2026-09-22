from pathlib import Path
import tempfile
import unittest

from scripts.browser_audio_receiver import CaptureState


class BrowserAudioReceiverTests(unittest.TestCase):
    def test_append_is_durable_and_stream_subscriber_keeps_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "capture.webm"
            output.touch()
            state = CaptureState(output, "token")

            state.append(b"header", 0)
            backlog_size, subscriber = state.subscribe()
            state.append(b"cluster", 1)

            self.assertEqual(backlog_size, len(b"header"))
            self.assertEqual(subscriber.get_nowait(), b"cluster")
            self.assertEqual(output.read_bytes(), b"headercluster")
            self.assertEqual(state.snapshot()["last_sequence"], 1)

    def test_rejects_missing_or_out_of_order_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "capture.webm"
            output.touch()
            state = CaptureState(output, "token")

            with self.assertRaisesRegex(ValueError, "empty chunk"):
                state.append(b"", 0)
            with self.assertRaisesRegex(ValueError, "unexpected sequence"):
                state.append(b"late", 1)


if __name__ == "__main__":
    unittest.main()
