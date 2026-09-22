from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

from live_sentinel.audio.archive import SegmentArchiveWriter
from live_sentinel.audio.finalizer import AudioFinalizer
from live_sentinel.audio.segment import TimelineGap
from live_sentinel.audio.source import (
    CaptureAuditBuffer,
    FFmpegAudioSource,
    IterableAudioSource,
    LiveAudioInterruptedError,
    NonSilenceProbeAudioSource,
    RoomAwareAudioSource,
    RoomStatusSnapshot,
    StartHookAudioSource,
)
from live_sentinel.models import AudioFrame


class ArchiveTests(unittest.TestCase):
    @staticmethod
    def _frames(*, count: int, silent: bool = False) -> list[AudioFrame]:
        sample = b"\x00\x00" if silent else b"\xe8\x03"
        return [
            AudioFrame(index * 100, sample * 100, 1000, 1, 2)
            for index in range(count)
        ]

    def test_room_guard_rejects_eof_while_room_is_live(self) -> None:
        audit = CaptureAuditBuffer()
        source = RoomAwareAudioSource(
            IterableAudioSource(self._frames(count=1)),
            lambda: RoomStatusSnapshot(True, "protected"),
            audit=audit,
            initial_access_mode="public",
            status_poll_interval_sec=30,
            offline_confirmations=2,
            offline_confirmation_interval_sec=1,
            silence_timeout_sec=30,
        )
        source.start()
        try:
            self.assertIsNotNone(source.read())
            with self.assertRaisesRegex(LiveAudioInterruptedError, "仍在直播"):
                source.read()
        finally:
            source.stop()
        event_types = [event.event_type for event in audit.drain()]
        self.assertIn("AUDIO_SOURCE_EOF_WHILE_LIVE", event_types)
        self.assertIn("STREAM_ACCESS_MODE_CHANGED", event_types)

    def test_room_guard_requires_consecutive_offline_confirmations(self) -> None:
        observations = iter(
            [
                RoomStatusSnapshot(False, "offline"),
                RoomStatusSnapshot(False, "offline"),
            ]
        )
        audit = CaptureAuditBuffer()
        source = RoomAwareAudioSource(
            IterableAudioSource(self._frames(count=25)),
            lambda: next(observations),
            audit=audit,
            initial_access_mode="public",
            status_poll_interval_sec=0.01,
            offline_confirmations=2,
            offline_confirmation_interval_sec=0.01,
            silence_timeout_sec=30,
        )
        source.start()
        try:
            while source.read() is not None:
                pass
        finally:
            source.stop()
        events = audit.drain()
        self.assertEqual(
            sum(event.event_type == "ROOM_OFFLINE_OBSERVED" for event in events),
            2,
        )
        self.assertEqual(events[-1].event_type, "ROOM_OFFLINE_CONFIRMED")

    def test_room_guard_fails_closed_on_continuous_silence_while_live(self) -> None:
        class SlowSource(IterableAudioSource):
            def read(self):
                time.sleep(0.01)
                return super().read()

        audit = CaptureAuditBuffer()
        source = RoomAwareAudioSource(
            SlowSource(self._frames(count=50, silent=True)),
            lambda: RoomStatusSnapshot(True, "protected"),
            audit=audit,
            initial_access_mode="public",
            status_poll_interval_sec=0.01,
            offline_confirmations=2,
            offline_confirmation_interval_sec=1,
            silence_timeout_sec=1,
            silence_rms_threshold=100,
        )
        source.start()
        try:
            with self.assertRaisesRegex(LiveAudioInterruptedError, "连续静音"):
                while True:
                    source.read()
        finally:
            source.stop()
        event_types = [event.event_type for event in audit.drain()]
        self.assertIn("AUDIO_SILENCE_WHILE_LIVE", event_types)

    def test_room_status_network_probe_does_not_block_audio_read(self) -> None:
        probe_started = threading.Event()
        release_probe = threading.Event()

        def slow_probe() -> RoomStatusSnapshot:
            probe_started.set()
            release_probe.wait(timeout=2)
            return RoomStatusSnapshot(True, "public")

        source = RoomAwareAudioSource(
            IterableAudioSource(self._frames(count=2)),
            slow_probe,
            audit=CaptureAuditBuffer(),
            initial_access_mode="public",
            status_poll_interval_sec=0.01,
            offline_confirmations=2,
            offline_confirmation_interval_sec=0.01,
            silence_timeout_sec=30,
        )
        source.start()
        try:
            self.assertTrue(probe_started.wait(timeout=0.5))
            started = time.monotonic()
            self.assertIsNotNone(source.read())
            self.assertLess(time.monotonic() - started, 0.1)
        finally:
            release_probe.set()
            source.stop()

    def test_start_hook_listens_before_opening_player_and_cleans_up_on_failure(self) -> None:
        events: list[str] = []

        class Source(IterableAudioSource):
            def start(self):
                events.append("source-start")
                super().start()

            def stop(self):
                events.append("source-stop")

        def failing_hook() -> None:
            events.append("browser-open")
            raise RuntimeError("open failed")

        source = StartHookAudioSource(Source([]), failing_hook)
        with self.assertRaisesRegex(RuntimeError, "open failed"):
            source.start()
        self.assertEqual(events, ["source-start", "browser-open", "source-stop"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
    def test_ffmpeg_source_reports_abnormal_eof(self) -> None:
        source = FFmpegAudioSource(
            "/definitely/missing/live-input.flv",
            sample_rate=1000,
            channels=1,
            frame_ms=100,
        )
        source.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "FFmpeg 音频输入异常退出"):
                source.read()
        finally:
            source.stop()

    def test_non_silence_probe_replays_buffer_without_losing_opening_audio(self) -> None:
        frames = [
            AudioFrame(0, b"\x00\x00" * 100, 1000, 1, 2),
            AudioFrame(100, b"\xe8\x03" * 100, 1000, 1, 2),
            AudioFrame(200, b"\xe8\x03" * 100, 1000, 1, 2),
        ]
        source = NonSilenceProbeAudioSource(
            IterableAudioSource(frames),
            probe_window_ms=300,
            rms_threshold=100,
            required_non_silent_ms=200,
        )
        source.start()
        observed = []
        while True:
            frame = source.read()
            if frame is None:
                break
            observed.append(frame)
        self.assertEqual([frame.timestamp_ms for frame in observed], [0, 100, 200])

    def test_non_silence_probe_fails_closed_on_silent_input(self) -> None:
        source = NonSilenceProbeAudioSource(
            IterableAudioSource(
                [
                    AudioFrame(0, b"\x00\x00" * 100, 1000, 1, 2),
                    AudioFrame(100, b"\x00\x00" * 100, 1000, 1, 2),
                ]
            ),
            probe_window_ms=200,
            rms_threshold=100,
            required_non_silent_ms=100,
        )
        source.start()
        with self.assertRaisesRegex(RuntimeError, "非静音探测失败"):
            source.read()

    @unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
    def test_ffmpeg_source_reads_authorized_input_as_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.wav"
            with wave.open(str(source_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(1000)
                handle.writeframes(b"\x01\x00" * 250)
            source = FFmpegAudioSource(str(source_path), sample_rate=1000, channels=1, frame_ms=100)
            source.start()
            frames = []
            while True:
                frame = source.read()
                if frame is None:
                    break
                frames.append(frame)
            source.stop()
            self.assertEqual([frame.timestamp_ms for frame in frames], [0, 100, 200])
            self.assertEqual(sum(frame.duration_ms for frame in frames), 250)

    def test_wav_segments_finalize_to_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = SegmentArchiveWriter(
                root / "working",
                session_id="demo",
                segment_minutes=1,
                sample_rate=1000,
                channels=1,
                sample_width=2,
                codec="wav",
            )
            writer.start()
            # 两帧共 61 秒，刚好切出两个 working segment。
            writer.write(AudioFrame(0, b"\x01\x00" * 60_000, 1000, 1, 2))
            writer.write(AudioFrame(60_000, b"\x01\x00" * 1_000, 1000, 1, 2))
            segments = writer.close()
            self.assertEqual(len(segments), 2)
            final = AudioFinalizer(root / "final", codec="wav").finalize(
                "demo", segments, session_duration_ms=61_000
            )
            self.assertEqual(final.file_path.suffix, ".wav")
            self.assertGreater(final.duration_ms, 60_000)
            self.assertEqual(len(final.checksum), 64)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg")
    def test_flac_segments_finalize_to_one_continuous_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = SegmentArchiveWriter(
                root / "working",
                session_id="flac-demo",
                segment_minutes=1,
                sample_rate=1_000,
                channels=1,
                sample_width=2,
                codec="flac",
            )
            writer.start()
            writer.write(AudioFrame(0, b"\x01\x00" * 60_000, 1_000, 1, 2))
            writer.write(AudioFrame(60_000, b"\x02\x00" * 1_000, 1_000, 1, 2))
            segments = writer.close()
            self.assertEqual(len(segments), 2)

            final = AudioFinalizer(root / "final", codec="flac").finalize(
                "flac-demo", segments, session_duration_ms=61_000
            )

            self.assertEqual(final.duration_ms, 61_000)
            self.assertEqual(final.file_path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list((root / "final").glob("*.partial.flac")))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg")
    def test_failed_finalize_does_not_replace_existing_final(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = SegmentArchiveWriter(
                root / "working",
                session_id="atomic",
                sample_rate=1_000,
                channels=1,
                sample_width=2,
                codec="flac",
            )
            writer.start()
            writer.write(AudioFrame(0, b"\x01\x00" * 1_000, 1_000, 1, 2))
            segments = writer.close()
            final_dir = root / "final"
            final_dir.mkdir()
            canonical = final_dir / "atomic.flac"
            canonical.write_bytes(b"known-good")

            with self.assertRaisesRegex(ValueError, "时长校验失败"):
                AudioFinalizer(final_dir, codec="flac").finalize(
                    "atomic", segments, session_duration_ms=10_000, tolerance_ms=1
                )

            self.assertEqual(canonical.read_bytes(), b"known-good")
            self.assertFalse(list(final_dir.glob("*.partial.flac")))

    def test_internal_source_gap_is_accounted_for(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = SegmentArchiveWriter(
                root / "working",
                session_id="gap",
                segment_minutes=1,
                sample_rate=1000,
                channels=1,
                sample_width=2,
                codec="wav",
            )
            writer.start()
            writer.write(AudioFrame(0, b"\x01\x00" * 100, 1000, 1, 2))
            writer.write(AudioFrame(200, b"\x01\x00" * 100, 1000, 1, 2))
            segments = writer.close()
            final = AudioFinalizer(root / "final", codec="wav").finalize(
                "gap",
                segments,
                session_duration_ms=300,
                gaps=[
                    # 100ms 的源中断发生在同一个 working segment 内。
                    TimelineGap(100, 200)
                ],
            )
            self.assertEqual(final.duration_ms, 200)
            self.assertEqual([(gap.start_ms, gap.end_ms) for gap in final.gaps], [(100, 200)])
