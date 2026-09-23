from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from live_sentinel.analysis.analyzer import RuleBasedContentAnalyzer
from live_sentinel.audio.archive import ArchiveError, SegmentArchiveWriter
from live_sentinel.audio.finalizer import AudioFinalizer
from live_sentinel.audio.resample import RealtimeAudioPreprocessor
from live_sentinel.audio.source import EmptyAudioSource, IterableAudioSource, OffsetAudioSource
from live_sentinel.asr.realtime import CallableStreamingASR, NullStreamingASR
from live_sentinel.asr.vad import EnergyVAD
from live_sentinel.config import AppConfig, AsrConfig, AudioConfig, NotificationConfig, RealtimeAsrConfig
from live_sentinel.bilibili import BilibiliRoomInfo
from live_sentinel.integrations import IntegrationBundle
from live_sentinel.models import AudioFrame, Session, SessionStatus, TranscriptSegment
from live_sentinel.session.checkpoint import CheckpointStore
from live_sentinel.session.manager import SessionManager
from live_sentinel.runner import run_bilibili_session
from live_sentinel.storage.artifacts import SessionArtifacts
from live_sentinel.storage.sqlite import SQLiteStore


class _StopAfterFrames(IterableAudioSource):
    def __init__(self, frames, stop_event):
        super().__init__(frames)
        self.stop_event = stop_event

    def read(self):
        frame = super().read()
        if frame is None:
            self.stop_event.set()
        return frame


class ResumeTests(unittest.TestCase):
    def test_runner_first_start_does_not_mistake_new_root_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1000, archive_channels=1,
                    archive_sample_width=2, archive_codec="wav",
                ),
                asr=AsrConfig(realtime=RealtimeAsrConfig(enabled=False)),
                notification=NotificationConfig(enabled=False),
            )
            config.storage.root_dir = directory
            config.storage.staging_dir = None
            config.storage.require_mounted_path = False
            config.storage.minimum_free_gib = 0
            info = BilibiliRoomInfo(
                room_id=5436512, requested_room_id=5436512,
                creator_uid="1", uname="up", title="live", live_status=1,
                stream_url="https://example.test/live.flv",
            )
            bundle = IntegrationBundle(
                realtime_asr=NullStreamingASR(), offline_asr=None,
                llm_judge=None, notifier=None, summary_notifier=None,
                feishu_docs=None, backup=None, realtime_name="none",
                offline_name="none", llm_name="none", notifier_name="none",
                summary_notifier_name="none", feishu_docs_name="none",
                backup_name="none",
            )
            frame = AudioFrame(0, b"\x01\x00" * 100, 1000, 1, 2)
            stop = threading.Event()
            with patch(
                "live_sentinel.runner.FFmpegAudioSource",
                return_value=_StopAfterFrames([frame], stop),
            ):
                paused = run_bilibili_session(
                    info, config, integrations=bundle, session_id="new-session",
                    stop_event=stop,
                )
            self.assertEqual(paused.result.session.status, SessionStatus.PAUSED)
            result = run_bilibili_session(
                replace(info, live_status=0, stream_url=None),
                config, integrations=bundle, session_id="new-session",
            )
            self.assertEqual(result.result.session.status, SessionStatus.COMPLETED)
            self.assertTrue((Path(directory) / "new-session" / "session.json").exists())

    def test_two_pauses_keep_one_session_then_offline_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1000, archive_channels=1,
                    archive_sample_width=2, archive_codec="wav",
                ),
                asr=AsrConfig(realtime=RealtimeAsrConfig(enabled=False)),
                notification=NotificationConfig(enabled=False),
            )
            store = SQLiteStore(root / "session.sqlite")
            checkpoint_store = CheckpointStore(root / "checkpoint.json")
            session = Session("same-id", "room", "up", "original-start")

            def run(source, checkpoint=None, stop=None):
                writer = SegmentArchiveWriter(root / "working", session.id, 1, 1000, 1, 2, "wav")
                if checkpoint is not None:
                    writer.restore(store.load_audio_segments(session.id))
                manager = SessionManager(
                    config, source, writer, RealtimeAudioPreprocessor(1000),
                    EnergyVAD(), CallableStreamingASR(lambda _speech: None),
                    RuleBasedContentAnalyzer([], []),
                    finalizer=AudioFinalizer(root / "final", codec="wav"),
                    store=store, artifacts=SessionArtifacts(root),
                    checkpoint_store=checkpoint_store,
                    realtime_enabled=False, resume_checkpoint=checkpoint,
                    stop_event=stop,
                )
                return manager.run(session)

            stop1 = threading.Event()
            frame = AudioFrame(0, b"\x01\x00" * 100, 1000, 1, 2)
            first = run(_StopAfterFrames([frame], stop1), stop=stop1)
            self.assertEqual(first.session.status, SessionStatus.PAUSED)
            self.assertEqual(store.count("audio_segments", session.id), 1)
            self.assertIsNone(session.end_time)
            self.assertIsNone(first.final_audio)
            store.add_realtime_transcript(
                session.id, TranscriptSegment(0, 100, "重启前的上下文")
            )
            checkpoint1 = checkpoint_store.load()
            self.assertIsNotNone(checkpoint1)
            time.sleep(0.02)

            stop2 = threading.Event()
            resumed = OffsetAudioSource(
                _StopAfterFrames([frame], stop2),
                checkpoint1.last_timestamp_ms, checkpoint1.paused_at,
            )
            second = run(resumed, checkpoint1, stop2)
            self.assertEqual(second.session.status, SessionStatus.PAUSED)
            self.assertEqual(second.realtime_transcripts[0].text, "重启前的上下文")
            segments = store.load_audio_segments(session.id)
            self.assertEqual(len(segments), 2)
            self.assertNotEqual(segments[0].file_path, segments[1].file_path)
            self.assertGreater(segments[1].start_ms, segments[0].end_ms)
            self.assertEqual(session.start_time, "original-start")
            self.assertIn("CAPTURE_GAP", [event.event_type for event in second.events])

            checkpoint2 = checkpoint_store.load()
            result = run(EmptyAudioSource(), checkpoint2)
            self.assertEqual(result.session.status, SessionStatus.COMPLETED)
            self.assertEqual(store.count("audio_segments", session.id), 2)
            self.assertIsNotNone(result.final_audio)
            self.assertEqual(result.final_audio.duration_ms, 200)
            self.assertEqual(len(result.final_audio.gaps), 1)
            self.assertEqual(store.load_session(session.id).start_time, "original-start")
            store.close()

    def test_restore_rejects_unindexed_segment_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "segment_000_000000000000.wav"
            path.write_bytes(b"unverified")
            writer = SegmentArchiveWriter(directory, "s", 1, 1000, 1, 2, "wav")
            with self.assertRaises(ArchiveError):
                writer.restore([])
            self.assertEqual(path.read_bytes(), b"unverified")


if __name__ == "__main__":
    unittest.main()
