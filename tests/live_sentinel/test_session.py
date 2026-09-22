from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from live_sentinel.analysis.analyzer import RuleBasedContentAnalyzer
from live_sentinel.audio.archive import SegmentArchiveWriter
from live_sentinel.audio.finalizer import AudioFinalizer
from live_sentinel.audio.resample import RealtimeAudioPreprocessor
from live_sentinel.audio.source import (
    AudioSource,
    CaptureAuditBuffer,
    IterableAudioSource,
    LiveAudioInterruptedError,
    RoomAwareAudioSource,
    RoomStatusSnapshot,
)
from live_sentinel.asr.realtime import CallableStreamingASR
from live_sentinel.asr.vad import EnergyVAD
from live_sentinel.config import (
    AppConfig,
    AnalysisConfig,
    AsrConfig,
    AudioConfig,
    InterestConfig,
    NotificationConfig,
    RealtimeAsrConfig,
)
from live_sentinel.models import (
    AnalysisFeatures,
    AudioFrame,
    SemanticResult,
    Session,
    SessionStatus,
    TranscriptSegment,
)
from live_sentinel.notification.notifier import MemoryNotifier
from live_sentinel.postprocess.pipeline import OfflineFinalizer
from live_sentinel.session.manager import SessionManager
from live_sentinel.storage.sqlite import SQLiteStore
from live_sentinel.storage.artifacts import SessionArtifacts


class SessionTests(unittest.TestCase):
    def test_live_eof_is_failed_and_audited_for_watchlist_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1000,
                    archive_channels=1,
                    archive_sample_width=2,
                    archive_codec="wav",
                ),
                asr=AsrConfig(realtime=RealtimeAsrConfig(enabled=False)),
                notification=NotificationConfig(enabled=False),
            )
            audit = CaptureAuditBuffer()
            source = RoomAwareAudioSource(
                IterableAudioSource(
                    [AudioFrame(0, b"\xe8\x03" * 100, 1000, 1, 2)]
                ),
                lambda: RoomStatusSnapshot(True, "protected"),
                audit=audit,
                initial_access_mode="public",
                status_poll_interval_sec=30,
                offline_confirmations=2,
                offline_confirmation_interval_sec=1,
                silence_timeout_sec=30,
            )
            store = SQLiteStore(root / "session.sqlite")
            manager = SessionManager(
                config,
                source,
                SegmentArchiveWriter(
                    root / "working", "live-eof", 1, 1000, 1, 2, "wav"
                ),
                RealtimeAudioPreprocessor(1000),
                EnergyVAD(),
                CallableStreamingASR(lambda _speech: None),
                RuleBasedContentAnalyzer([], []),
                store=store,
                artifacts=SessionArtifacts(root),
                realtime_enabled=False,
                capture_audit=audit,
            )
            session = Session("live-eof", "room", "up", "now")
            with self.assertRaises(LiveAudioInterruptedError):
                manager.run(session)
            self.assertEqual(session.status, SessionStatus.FAILED)
            rows = store._connection.execute(
                "SELECT event_type FROM events WHERE session_id = ? ORDER BY id",
                (session.id,),
            ).fetchall()
            self.assertIn(
                "AUDIO_SOURCE_EOF_WHILE_LIVE",
                [row[0] for row in rows],
            )
            store.close()

    def test_capture_audit_events_persist_when_offline_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1000,
                    archive_channels=1,
                    archive_sample_width=2,
                    archive_codec="wav",
                ),
                asr=AsrConfig(realtime=RealtimeAsrConfig(enabled=False)),
                notification=NotificationConfig(enabled=False),
            )
            audit = CaptureAuditBuffer()
            source = RoomAwareAudioSource(
                IterableAudioSource(
                    [AudioFrame(0, b"\xe8\x03" * 100, 1000, 1, 2)]
                ),
                lambda: RoomStatusSnapshot(False, "offline"),
                audit=audit,
                initial_access_mode="public",
                status_poll_interval_sec=30,
                offline_confirmations=1,
                offline_confirmation_interval_sec=1,
                silence_timeout_sec=30,
            )
            store = SQLiteStore(root / "session.sqlite")
            manager = SessionManager(
                config,
                source,
                SegmentArchiveWriter(
                    root / "working", "capture-audit", 1, 1000, 1, 2, "wav"
                ),
                RealtimeAudioPreprocessor(1000),
                EnergyVAD(),
                CallableStreamingASR(lambda _speech: None),
                RuleBasedContentAnalyzer([], []),
                finalizer=AudioFinalizer(root / "final", codec="wav"),
                store=store,
                artifacts=SessionArtifacts(root),
                realtime_enabled=False,
                capture_audit=audit,
            )
            result = manager.run(Session("capture-audit", "room", "up", "now"))
            event_types = [event.event_type for event in result.events]
            self.assertIn("CAPTURE_MODE_SELECTED", event_types)
            self.assertIn("ROOM_OFFLINE_CONFIRMED", event_types)
            self.assertGreaterEqual(store.count("events", "capture-audit"), 2)
            self.assertIn(
                "ROOM_OFFLINE_CONFIRMED",
                (root / "realtime" / "events.jsonl").read_text(encoding="utf-8"),
            )
            store.close()

    def test_deferred_delivery_does_not_publish_or_notify_before_gate(self) -> None:
        class Docs:
            def __init__(self):
                self.calls = 0

            def upload_session(self, _session, _final_dir):
                self.calls += 1
                return []

        class Summary:
            def __init__(self):
                self.messages: list[str] = []

            def send_text(self, text: str):
                self.messages.append(text)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1000,
                    archive_channels=1,
                    archive_sample_width=2,
                    archive_codec="wav",
                ),
                asr=AsrConfig(realtime=RealtimeAsrConfig(enabled=False)),
                notification=NotificationConfig(enabled=False),
            )
            docs = Docs()
            summary = Summary()
            manager = SessionManager(
                config,
                IterableAudioSource([AudioFrame(0, b"\x01\x00" * 100, 1000, 1, 2)]),
                SegmentArchiveWriter(root / "working", "delivery-gate", 1, 1000, 1, 2, "wav"),
                RealtimeAudioPreprocessor(1000),
                EnergyVAD(),
                CallableStreamingASR(lambda _speech: None),
                RuleBasedContentAnalyzer([], []),
                finalizer=AudioFinalizer(root / "audio-final", codec="wav"),
                offline_finalizer=OfflineFinalizer(root / "final"),
                artifacts=SessionArtifacts(root),
                feishu_docs=docs,
                summary_notifier=summary,
                realtime_enabled=False,
                defer_delivery=True,
            )
            session = Session("delivery-gate", "room", "up", "now")
            result = manager.run(session)
            self.assertEqual(session.status, SessionStatus.DELIVERY_PENDING)
            self.assertEqual(docs.calls, 0)
            self.assertEqual(summary.messages, [])

            manager.retain_in_staging(result, RuntimeError("injected T5 unavailable"))
            self.assertEqual(session.status, SessionStatus.RETAINED_IN_STAGING)
            self.assertEqual(docs.calls, 0)
            self.assertIn("主归档尚未发布", summary.messages[-1])
            self.assertIn("RETAINED_IN_STAGING", summary.messages[-1])

            summary.messages.clear()
            session.status = SessionStatus.DELIVERY_PENDING
            manager.complete_delivery(result)
            self.assertEqual(session.status, SessionStatus.COMPLETED)
            self.assertEqual(docs.calls, 1)
            self.assertEqual(len(summary.messages), 1)
            self.assertIn("直播归档完成", summary.messages[0])

    def test_slow_judge_and_failed_notification_do_not_block_capture_reads(self) -> None:
        class TrackingSource(IterableAudioSource):
            def __init__(self, frames):
                super().__init__(frames)
                self.finished = threading.Event()
                self.finished_at: float | None = None

            def read(self):
                frame = super().read()
                if frame is None:
                    self.finished_at = time.monotonic()
                    self.finished.set()
                return frame

        class AlwaysInterestingAnalyzer:
            def analyze(self, *_args, **_kwargs):
                return AnalysisFeatures(
                    topic="AI",
                    whitelist_score=1.0,
                    information_density=1.0,
                )

        class BlockingJudge:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()
                self.finished_at: float | None = None

            def evaluate(self, _context, _features):
                self.started.set()
                self.release.wait(timeout=5)
                self.finished_at = time.monotonic()
                return SemanticResult(topic="AI", summary="canary", score=1.0)

        class OfflineNotifier:
            def send(self, _event, _highlight=None):
                time.sleep(0.05)
                raise RuntimeError("injected ntfy offline")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = [
                AudioFrame(index * 100, b"\xe8\x03" * 100, 1000, 1, 2)
                for index in range(40)
            ]
            source = TrackingSource(frames)
            judge = BlockingJudge()
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1000,
                    archive_channels=1,
                    archive_sample_width=2,
                    archive_codec="wav",
                    segment_minutes=1,
                ),
                analysis=AnalysisConfig(interval_sec=1, llm_window_sec=3, context_window_sec=5),
                interest=InterestConfig(
                    whitelist=["AI"],
                    blacklist=[],
                    candidate_threshold=0.1,
                    hot_threshold=0.1,
                    enter_hot_consecutive_windows=1,
                    notification_cooldown_sec=0,
                ),
                notification=NotificationConfig(enabled=True),
            )
            manager = SessionManager(
                config,
                source,
                SegmentArchiveWriter(root / "working", "isolation", 1, 1000, 1, 2, "wav"),
                RealtimeAudioPreprocessor(1000),
                EnergyVAD(rms_threshold=100, silence_ms=100),
                CallableStreamingASR(
                    lambda speech: TranscriptSegment(speech.start_ms, speech.end_ms, "AI")
                ),
                AlwaysInterestingAnalyzer(),
                finalizer=AudioFinalizer(root / "final", codec="wav"),
                llm_judge=judge,
                notifier=OfflineNotifier(),
            )
            holder: dict[str, object] = {}

            def run() -> None:
                holder["result"] = manager.run(Session("isolation", "room", "up", "now"))

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(judge.started.wait(timeout=2))
            self.assertTrue(source.finished.wait(timeout=1))
            self.assertIsNotNone(source.finished_at)
            self.assertIsNone(judge.finished_at)
            judge.release.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            result = holder["result"]
            self.assertEqual(result.session.status.value, "COMPLETED")
            self.assertEqual(result.final_audio.duration_ms, 4000)
            self.assertTrue(
                any(event.event_type == "NOTIFICATION_ERROR" for event in result.events)
            )

    def test_realtime_pipeline_can_be_paused_without_affecting_archive_or_offline_asr(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1000,
                    archive_channels=1,
                    archive_sample_width=2,
                    archive_codec="wav",
                    segment_minutes=1,
                ),
                asr=AsrConfig(realtime=RealtimeAsrConfig(enabled=False)),
                analysis=AnalysisConfig(interval_sec=1, llm_window_sec=3, context_window_sec=3),
                interest=InterestConfig(whitelist=["AI"], blacklist=[], notification_cooldown_sec=600),
                notification=NotificationConfig(enabled=True),
            )
            notifier = MemoryNotifier()
            offline_calls: list[Path] = []

            def offline_asr(path: Path) -> list[TranscriptSegment]:
                offline_calls.append(path)
                return [TranscriptSegment(0, 100, "离线转写仍然运行")]

            def unexpected_realtime(_speech: object) -> None:
                raise AssertionError("实时链路关闭后不应调用 Streaming ASR")

            manager = SessionManager(
                config,
                IterableAudioSource([AudioFrame(0, b"\x01\x00" * 100, 1000, 1, 2)]),
                SegmentArchiveWriter(root / "working", "paused", 1, 1000, 1, 2, "wav"),
                RealtimeAudioPreprocessor(1000),
                EnergyVAD(rms_threshold=100, silence_ms=100),
                CallableStreamingASR(unexpected_realtime),
                RuleBasedContentAnalyzer(["AI"], []),
                finalizer=AudioFinalizer(root / "audio-final", codec="wav"),
                notifier=notifier,
                offline_finalizer=OfflineFinalizer(root / "final", offline_asr=offline_asr),
                artifacts=SessionArtifacts(root),
            )
            session = Session("paused", "room", "主播", datetime.now(timezone.utc).isoformat())
            result = manager.run(session)
            self.assertEqual(session.status.value, "COMPLETED")
            self.assertIsNotNone(result.final_audio)
            self.assertEqual(result.realtime_transcripts, [])
            stage_events = [event for event in result.events if event.event_type.startswith("STAGE_")]
            self.assertTrue(stage_events)
            self.assertTrue(
                any(
                    event.event_type == "STAGE_SKIPPED"
                    and event.payload.get("stage") == "realtime_asr"
                    for event in stage_events
                )
            )
            self.assertFalse(any(event.event_type == "ANALYSIS" for event in result.events))
            self.assertEqual(notifier.events, [])
            self.assertEqual(len(offline_calls), 1)
            self.assertIn("离线转写仍然运行", (root / "final" / "readable.md").read_text())
            self.assertTrue(manager.tee.realtime_queue.empty())
            self.assertEqual(manager.tee.stats.realtime_dropped, 0)

    def test_unavailable_realtime_adapter_can_disable_empty_analysis_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1_000,
                    archive_channels=1,
                    archive_sample_width=2,
                    archive_codec="wav",
                ),
                analysis=AnalysisConfig(interval_sec=1),
            )
            manager = SessionManager(
                config,
                IterableAudioSource([AudioFrame(0, b"\x00\x00" * 2_000, 1_000, 1, 2)]),
                SegmentArchiveWriter(root / "working", "no-realtime", 1, 1_000, 1, 2, "wav"),
                RealtimeAudioPreprocessor(1_000),
                EnergyVAD(),
                CallableStreamingASR(lambda _speech: None),
                RuleBasedContentAnalyzer([], []),
                finalizer=AudioFinalizer(root / "final", codec="wav"),
                realtime_enabled=False,
            )
            result = manager.run(
                Session("no-realtime", "room", "主播", datetime.now(timezone.utc).isoformat())
            )
            self.assertFalse(any(event.event_type == "ANALYSIS" for event in result.events))
            self.assertTrue(
                any(
                    event.event_type == "STAGE_SKIPPED"
                    and event.payload.get("stage") == "realtime_asr"
                    for event in result.events
                )
            )
            self.assertTrue(manager.tee.realtime_queue.empty())

    def test_pre_first_frame_failure_is_staged_and_redacted(self) -> None:
        class FailingSource(AudioSource):
            def start(self) -> None:
                raise RuntimeError("api_key=sk-secret-value-12345678")

            def read(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1_000,
                    archive_channels=1,
                    archive_sample_width=2,
                    archive_codec="wav",
                ),
                asr=AsrConfig(realtime=RealtimeAsrConfig(enabled=False)),
            )
            store = SQLiteStore(root / "session.sqlite")
            manager = SessionManager(
                config,
                FailingSource(),
                SegmentArchiveWriter(root / "working", "startup-fail", 1, 1_000, 1, 2, "wav"),
                RealtimeAudioPreprocessor(1_000),
                EnergyVAD(),
                CallableStreamingASR(lambda _speech: None),
                RuleBasedContentAnalyzer([], []),
                store=store,
                artifacts=SessionArtifacts(root),
                realtime_enabled=False,
            )
            session = Session("startup-fail", "room", "主播", datetime.now(timezone.utc).isoformat())
            with self.assertRaises(RuntimeError):
                manager.run(session)
            self.assertEqual(session.status, SessionStatus.FAILED)
            events = store._connection.execute(
                "SELECT event_type, payload FROM events ORDER BY id"
            ).fetchall()
            self.assertEqual(events[0][0], "STAGE_STARTED")
            self.assertEqual(events[-1][0], "STAGE_FAILED")
            persisted = "\n".join(str(row[1]) for row in events)
            self.assertIn("[REDACTED]", persisted)
            self.assertNotIn("sk-secret-value", persisted)
            self.assertNotIn("sk-secret-value", (root / "realtime/events.jsonl").read_text())
            store.close()

    def test_offline_transcript_replacement_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "session.sqlite")
            session = Session(
                "replace-transcript",
                "room",
                "主播",
                datetime.now(timezone.utc).isoformat(),
            )
            store.create_session(session)
            transcripts = [TranscriptSegment(0, 1_000, "第一次")]
            store.replace_final_transcripts(session.id, transcripts)
            store.replace_final_transcripts(session.id, transcripts)
            self.assertEqual(store.count("final_transcripts", session.id), 1)
            chapters = [
                {
                    "id": "chapter-1",
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "title": "开场",
                    "summary": "摘要",
                }
            ]
            store.replace_chapters(session.id, chapters)
            store.replace_chapters(session.id, chapters)
            row = store._connection.execute(
                "SELECT COUNT(*) FROM chapters WHERE session_id = ?", (session.id,)
            ).fetchone()
            self.assertEqual(row[0], 1)
            store.close()

    def test_mock_session_runs_to_completed_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1000,
                    archive_channels=1,
                    archive_sample_width=2,
                    archive_codec="wav",
                    segment_minutes=1,
                ),
                analysis=AnalysisConfig(
                    interval_sec=1,
                    fast_window_sec=1,
                    topic_window_sec=3,
                    llm_window_sec=3,
                    context_window_sec=5,
                ),
                interest=InterestConfig(
                    whitelist=["AI"],
                    blacklist=["星座"],
                    candidate_threshold=0.5,
                    hot_threshold=0.6,
                    leave_hot_threshold=0.2,
                    enter_hot_consecutive_windows=2,
                    leave_hot_consecutive_windows=2,
                    notification_cooldown_sec=0,
                ),
                notification=NotificationConfig(enabled=True, cooldown_minutes=0),
            )
            # 每帧 100ms，奇数帧有能量，偶数帧静音；VAD 会不断产出可识别语音段。
            frames = []
            for index in range(30):
                timestamp = index * 100
                pcm = (b"\xe8\x03" * 100) if index % 2 == 0 else (b"\x00\x00" * 100)
                frames.append(AudioFrame(timestamp, pcm, 1000, 1, 2))
            source = IterableAudioSource(frames)
            writer = SegmentArchiveWriter(
                root / "audio" / "working",
                "session-1",
                segment_minutes=1,
                sample_rate=1000,
                channels=1,
                sample_width=2,
                codec="wav",
            )
            store = SQLiteStore(root / "session.sqlite")
            notifier = MemoryNotifier()

            def transcribe(speech):
                return TranscriptSegment(speech.start_ms, speech.end_ms, "AI 第一 原因 方法")

            manager = SessionManager(
                config,
                source,
                writer,
                RealtimeAudioPreprocessor(1000),
                EnergyVAD(rms_threshold=100, silence_ms=100),
                CallableStreamingASR(transcribe),
                RuleBasedContentAnalyzer(config.interest.whitelist, config.interest.blacklist),
                finalizer=AudioFinalizer(root / "audio" / "final", codec="wav"),
                notifier=notifier,
                store=store,
                offline_finalizer=OfflineFinalizer(root / "final"),
                artifacts=SessionArtifacts(root),
            )
            session = Session(
                id="session-1",
                room_id="room-1",
                up_name="测试主播",
                start_time=datetime.now(timezone.utc).isoformat(),
            )
            result = manager.run(session)
            self.assertEqual(session.status.value, "COMPLETED")
            self.assertIsNotNone(result.final_audio)
            self.assertGreaterEqual(len(result.realtime_transcripts), 5)
            self.assertTrue(any(event.event_type == "ENTER_HOT" for event in result.events))
            self.assertEqual(len(notifier.events), 1)
            self.assertTrue((root / "final" / "verbatim.md").exists())
            self.assertTrue((root / "realtime" / "transcript.jsonl").exists())
            self.assertTrue((root / "realtime" / "analysis.jsonl").exists())
            self.assertTrue((root / "realtime" / "events.jsonl").exists())
            self.assertIn('"status": "COMPLETED"', (root / "session.json").read_text(encoding="utf-8"))
            session_payload = (root / "final" / "session.json").read_text(encoding="utf-8")
            self.assertIn('"status": "COMPLETED"', session_payload)
            self.assertEqual(store.count("realtime_transcripts", "session-1"), len(result.realtime_transcripts))
            store.close()

    def test_realtime_asr_failure_does_not_abort_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1000,
                    archive_channels=1,
                    archive_sample_width=2,
                    archive_codec="wav",
                    segment_minutes=1,
                ),
                analysis=AnalysisConfig(interval_sec=1, llm_window_sec=3, context_window_sec=3),
                interest=InterestConfig(whitelist=["AI"], blacklist=[]),
                notification=NotificationConfig(enabled=False),
            )
            frames = [
                AudioFrame(0, b"\xe8\x03" * 100, 1000, 1, 2),
                AudioFrame(100, b"\x00\x00" * 100, 1000, 1, 2),
                AudioFrame(200, b"\x00\x00" * 100, 1000, 1, 2),
            ]

            def failing_asr(_speech):
                raise RuntimeError("mock ASR down")

            manager = SessionManager(
                config,
                IterableAudioSource(frames),
                SegmentArchiveWriter(root / "working", "broken-asr", 1, 1000, 1, 2, "wav"),
                RealtimeAudioPreprocessor(1000),
                EnergyVAD(rms_threshold=100, silence_ms=100),
                CallableStreamingASR(failing_asr),
                RuleBasedContentAnalyzer(["AI"], []),
                finalizer=AudioFinalizer(root / "final-audio", codec="wav"),
                realtime_join_timeout_sec=2,
            )
            session = Session(
                id="broken-asr",
                room_id="room-1",
                up_name="测试主播",
                start_time=datetime.now(timezone.utc).isoformat(),
            )
            result = manager.run(session)
            self.assertEqual(session.status.value, "COMPLETED")
            self.assertIsNotNone(result.final_audio)
            self.assertTrue(any(event.event_type == "ASR_ERROR" for event in result.events))

    def test_session_scoped_offline_asr_is_started_and_stopped_with_task(self) -> None:
        class SessionScopedAsr:
            start_mode = "session"

            def __init__(self):
                self.started = 0
                self.stopped = 0

            def start(self):
                self.started += 1

            def stop(self):
                self.stopped += 1

            def __call__(self, _path):
                return []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                audio=AudioConfig(
                    archive_sample_rate=1000,
                    archive_channels=1,
                    archive_sample_width=2,
                    archive_codec="wav",
                    segment_minutes=1,
                ),
                analysis=AnalysisConfig(interval_sec=1, llm_window_sec=3, context_window_sec=3),
                notification=NotificationConfig(enabled=False),
            )
            local_asr = SessionScopedAsr()
            manager = SessionManager(
                config,
                IterableAudioSource([AudioFrame(0, b"\x00\x00" * 100, 1000, 1, 2)]),
                SegmentArchiveWriter(root / "working", "session-scoped", 1, 1000, 1, 2, "wav"),
                RealtimeAudioPreprocessor(1000),
                EnergyVAD(rms_threshold=100, silence_ms=100),
                CallableStreamingASR(lambda _speech: None),
                RuleBasedContentAnalyzer([], []),
                finalizer=AudioFinalizer(root / "audio-final", codec="wav"),
                offline_finalizer=OfflineFinalizer(root / "final", offline_asr=local_asr),
            )
            session = Session(
                id="session-scoped",
                room_id="room-1",
                up_name="测试主播",
                start_time=datetime.now(timezone.utc).isoformat(),
            )
            manager.run(session)
            self.assertEqual(local_asr.started, 1)
            self.assertEqual(local_asr.stopped, 1)
