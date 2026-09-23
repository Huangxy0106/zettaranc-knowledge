"""V1 Session 主循环。

编排器只负责连接各模块和维护优先级，不把具体 B 站抓流、ASR、LLM 实现写死。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Empty, Full, Queue
import json
from pathlib import Path
import threading
from typing import Any

from ..analysis.llm_judge import LLMJudge
from ..audio.finalizer import AudioFinalizer, FinalAudio
from ..audio.source import CaptureAuditBuffer
from ..audio.segment import TimelineGap
from ..audio.tee import AudioTee
from ..backup.baidu import BaiduNetdiskBackup
from ..cloud.feishu import FeishuCloudDocUploader
from ..config import AppConfig
from ..interest.scorer import InterestScorer
from ..interest.state_machine import InterestStateMachine
from ..models import (
    AnalysisFeatures,
    InterestEvent,
    Session,
    SessionStatus,
    TranscriptSegment,
)
from ..notification.notifier import Notifier
from ..observability import redact_payload, redact_text
from ..storage.artifacts import SessionArtifacts
from .checkpoint import Checkpoint, CheckpointStore
from ..transcript.buffer import TranscriptBuffer


@dataclass
class SessionResult:
    session: Session
    final_audio: FinalAudio | None = None
    highlights: list[Any] = field(default_factory=list)
    events: list[InterestEvent] = field(default_factory=list)
    realtime_transcripts: list[TranscriptSegment] = field(default_factory=list)


class SessionManager:
    def __init__(
        self,
        config: AppConfig,
        audio_source: Any,
        archive_writer: Any,
        realtime_preprocessor: Any,
        vad: Any,
        realtime_asr: Any,
        analyzer: Any,
        *,
        finalizer: AudioFinalizer | None = None,
        llm_judge: LLMJudge | None = None,
        notifier: Notifier | None = None,
        summary_notifier: Any | None = None,
        feishu_docs: FeishuCloudDocUploader | None = None,
        backup: BaiduNetdiskBackup | None = None,
        store: Any | None = None,
        tee: AudioTee | None = None,
        transcript_buffer: TranscriptBuffer | None = None,
        scorer: InterestScorer | None = None,
        state_machine: InterestStateMachine | None = None,
        offline_finalizer: Any | None = None,
        artifacts: SessionArtifacts | None = None,
        realtime_join_timeout_sec: float = 5.0,
        checkpoint_store: CheckpointStore | None = None,
        checkpoint_interval_sec: int = 30,
        realtime_enabled: bool | None = None,
        defer_delivery: bool = False,
        capture_audit: CaptureAuditBuffer | None = None,
        resume_checkpoint: Checkpoint | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.config = config
        self.audio_source = audio_source
        self.archive_writer = archive_writer
        self.realtime_preprocessor = realtime_preprocessor
        self.vad = vad
        self.realtime_asr = realtime_asr
        self.analyzer = analyzer
        self.finalizer = finalizer
        self.llm_judge = llm_judge
        self.notifier = notifier
        self.summary_notifier = summary_notifier
        self.feishu_docs = feishu_docs
        self.backup = backup
        self.store = store
        self.tee = tee or AudioTee()
        self.transcript_buffer = transcript_buffer or TranscriptBuffer(
            config.analysis.context_window_sec
        )
        self.scorer = scorer or InterestScorer(config.interest)
        self.state_machine = state_machine
        self.offline_finalizer = offline_finalizer
        self.artifacts = artifacts
        if realtime_join_timeout_sec < 0:
            raise ValueError("realtime_join_timeout_sec 不能为负数")
        self.realtime_join_timeout_sec = realtime_join_timeout_sec
        if checkpoint_interval_sec <= 0:
            raise ValueError("checkpoint_interval_sec 必须为正数")
        self.checkpoint_store = checkpoint_store
        self.checkpoint_interval_ms = checkpoint_interval_sec * 1000
        self.defer_delivery = defer_delivery
        self.capture_audit = capture_audit
        self.resume_checkpoint = resume_checkpoint
        self.stop_event = stop_event
        self._state_lock = threading.RLock()
        realtime_provider = config.asr.realtime.provider.strip().lower()
        configured_realtime = bool(
            config.asr.realtime.enabled
            and realtime_provider not in {"none", "null", "disabled"}
        )
        self.realtime_enabled = (
            configured_realtime
            if realtime_enabled is None
            else configured_realtime and realtime_enabled
        )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _should_evaluate_semantics(
        features: AnalysisFeatures,
        base_score: float,
        semantic_trigger_threshold: float,
    ) -> bool:
        """优先主题必须交给 LLM 判断，不能被较高的规则阈值提前过滤。"""

        whitelist_matches = features.metadata.get("whitelist_matches", ())
        return bool(
            whitelist_matches
            or features.blacklist_score > 0
            or base_score >= semantic_trigger_threshold
        )

    def _persist_event(self, session_id: str, event: InterestEvent, result: SessionResult) -> None:
        safe_event = InterestEvent(
            event.event_type,
            event.timestamp_ms,
            event.state,
            event.score,
            highlight_id=event.highlight_id,
            payload=redact_payload(event.payload),
        )
        with self._state_lock:
            result.events.append(safe_event)
        if self.store is not None:
            self.store.add_event(session_id, safe_event)
        if self.artifacts is not None:
            if safe_event.event_type == "ANALYSIS":
                self.artifacts.append_analysis(safe_event)
            else:
                self.artifacts.append_event(safe_event)

    def record_stage(
        self,
        result: SessionResult,
        stage: str,
        state: str,
        **detail: Any,
    ) -> None:
        """Persist one event from the shared, deliberately small stage model."""

        normalized = state.strip().upper()
        if normalized not in {"STARTED", "SUCCEEDED", "FAILED", "SKIPPED"}:
            raise ValueError(f"无效阶段状态: {state}")
        session = result.session
        self._persist_event(
            session.id,
            InterestEvent(
                f"STAGE_{normalized}",
                session.duration_ms,
                self.state_machine.state,
                0.0,
                payload={"stage": stage, "at": self._now_iso(), **detail},
            ),
            result,
        )

    def _runtime_error(
        self,
        session: Session,
        timestamp_ms: int,
        event_type: str,
        error: Exception,
        result: SessionResult,
    ) -> None:
        """记录可降级模块异常；此方法本身不改变 P0 归档控制流。"""

        self._persist_event(
            session.id,
            InterestEvent(
                event_type,
                timestamp_ms,
                self.state_machine.state,
                0.0,
                payload={
                    "error": type(error).__name__,
                    "message": redact_text(error),
                },
            ),
            result,
        )

    def _drain_capture_audit(
        self,
        session: Session,
        result: SessionResult,
    ) -> None:
        if self.capture_audit is None:
            return
        for captured in self.capture_audit.drain():
            self._persist_event(
                session.id,
                InterestEvent(
                    captured.event_type,
                    captured.timestamp_ms,
                    self.state_machine.state,
                    0.0,
                    payload=captured.payload,
                ),
                result,
            )

    def _append_transcript(
        self,
        session: Session,
        transcript: TranscriptSegment,
        result: SessionResult,
    ) -> None:
        with self._state_lock:
            self.transcript_buffer.append(transcript)
            result.realtime_transcripts.append(transcript)
        if self.store is not None:
            self.store.add_realtime_transcript(session.id, transcript)
        if self.artifacts is not None:
            self.artifacts.append_transcript(transcript)

    def _run_realtime_worker(self, session: Session, result: SessionResult, stop: threading.Event) -> None:
        """后台消费 realtime queue；它永远不持有归档写入锁。"""

        disabled = False
        while not stop.is_set() or not self.tee.realtime_queue.empty():
            try:
                realtime_frame = self.tee.realtime_queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                if disabled:
                    continue
                try:
                    prepared = self.realtime_preprocessor.process(realtime_frame)
                    speech_segments = self.vad.process(prepared)
                except Exception as exc:
                    disabled = True
                    self._runtime_error(
                        session,
                        realtime_frame.end_ms,
                        "REALTIME_PIPELINE_ERROR",
                        exc,
                        result,
                    )
                    continue
                for speech in speech_segments:
                    try:
                        transcript = self.realtime_asr.transcribe(speech)
                    except Exception as exc:
                        self._runtime_error(session, speech.end_ms, "ASR_ERROR", exc, result)
                        continue
                    if transcript is not None:
                        self._append_transcript(session, transcript, result)
            finally:
                self.tee.realtime_queue.task_done()
        if disabled:
            return
        try:
            flushed_speech = self.vad.flush()
        except Exception as exc:
            self._runtime_error(session, session.duration_ms, "VAD_FLUSH_ERROR", exc, result)
            return
        for speech in flushed_speech:
            try:
                transcript = self.realtime_asr.transcribe(speech)
            except Exception as exc:
                self._runtime_error(session, speech.end_ms, "ASR_ERROR", exc, result)
                continue
            if transcript is not None:
                self._append_transcript(session, transcript, result)

    def _analyze(self, session: Session, timestamp_ms: int, result: SessionResult) -> None:
        with self._state_lock:
            context = self.transcript_buffer.last(
                self.config.analysis.llm_window_sec,
                now_ms=timestamp_ms,
            )
        features: AnalysisFeatures = self.analyzer.analyze(
            context,
            window_duration_ms=self.config.analysis.llm_window_sec * 1000,
        )
        base_score = self.scorer.score(features)
        semantic = None
        if self.llm_judge is not None and self._should_evaluate_semantics(
            features,
            base_score,
            self.config.interest.semantic_trigger_threshold,
        ):
            try:
                semantic = self.llm_judge.evaluate(context, features)
            except Exception as exc:
                self._runtime_error(session, timestamp_ms, "LLM_ERROR", exc, result)
        score = self.scorer.score(features, semantic)
        analysis_event = InterestEvent(
            "ANALYSIS",
            timestamp_ms,
            self.state_machine.state,
            score,
            payload={
                "topic": features.topic,
                "features": {
                    "whitelist_score": features.whitelist_score,
                    "blacklist_score": features.blacklist_score,
                    "information_density": features.information_density,
                    "structured_speech_score": features.structured_speech_score,
                    "topic_continuity": features.topic_continuity,
                    "novelty": features.novelty,
                    "speech_ratio": features.speech_ratio,
                    "whitelist_matches": list(
                        features.metadata.get("whitelist_matches", ())
                    ),
                    "blacklist_matches": list(
                        features.metadata.get("blacklist_matches", ())
                    ),
                },
                "semantic": semantic.summary if semantic else None,
                "semantic_topic": semantic.topic if semantic else None,
                "semantic_score": semantic.score if semantic else None,
                "semantic_high_information_density": (
                    semantic.is_high_information_density if semantic else None
                ),
            },
        )
        self._persist_event(session.id, analysis_event, result)
        event = self.state_machine.update(
            timestamp_ms,
            score,
            topic=semantic.topic if semantic and semantic.topic else features.topic,
            summary=semantic.summary if semantic else "",
        )
        if event is None:
            return
        self._persist_event(session.id, event, result)
        if self.store is not None:
            for highlight in self.state_machine.highlights:
                self.store.upsert_highlight(highlight)
        if (
            event.event_type == "ENTER_HOT"
            and event.payload.get("notify", False)
            and self.config.notification.enabled
            and self.notifier is not None
        ):
            try:
                self.notifier.send(event, self.state_machine.active_highlight)
            except Exception as exc:
                self._runtime_error(session, timestamp_ms, "NOTIFICATION_ERROR", exc, result)

    def _run_analysis_worker(
        self,
        session: Session,
        result: SessionResult,
        tasks: Queue[int],
        stop: threading.Event,
    ) -> None:
        """串行执行 P1 分析和网络通知；不得反压 P0 音频采集。"""

        while not stop.is_set() or not tasks.empty():
            try:
                timestamp_ms = tasks.get(timeout=0.1)
            except Empty:
                continue
            try:
                self._analyze(session, timestamp_ms, result)
            except Exception as exc:
                self._runtime_error(session, timestamp_ms, "ANALYSIS_ERROR", exc, result)
            finally:
                tasks.task_done()

    def _enqueue_latest_analysis(
        self,
        session: Session,
        timestamp_ms: int,
        result: SessionResult,
        tasks: Queue[int],
    ) -> None:
        """有界 latest-wins 队列；分析过载只能丢 P1 任务，不能阻塞归档。"""

        try:
            tasks.put_nowait(timestamp_ms)
            return
        except Full:
            pass
        try:
            dropped_timestamp_ms = tasks.get_nowait()
            tasks.task_done()
        except Empty:
            dropped_timestamp_ms = timestamp_ms
        self._persist_event(
            session.id,
            InterestEvent(
                "ANALYSIS_DROPPED",
                timestamp_ms,
                self.state_machine.state,
                0.0,
                payload={
                    "reason": "analysis_queue_full",
                    "dropped_timestamp_ms": dropped_timestamp_ms,
                },
            ),
            result,
        )
        tasks.put_nowait(timestamp_ms)

    def _upload_cloud_documents(self, session: Session, result: SessionResult) -> None:
        if self.feishu_docs is None:
            self.record_stage(result, "feishu_delivery", "SKIPPED", reason="disabled")
            return
        if self.offline_finalizer is None:
            self.record_stage(result, "feishu_delivery", "SKIPPED", reason="no_finalizer")
            return
        final_dir = getattr(self.offline_finalizer, "output_dir", None)
        if final_dir is None or not Path(final_dir).exists():
            self.record_stage(result, "feishu_delivery", "FAILED", reason="missing_final_dir")
            return
        self.record_stage(result, "feishu_delivery", "STARTED")
        try:
            documents = self.feishu_docs.upload_session(session, final_dir)
            payload = [
                {
                    "document_id": document.document_id,
                    "title": document.title,
                    "url": document.url,
                    "folder_token": document.folder_token,
                    "delivery_state": document.delivery_state,
                    "owner_type": document.owner_type,
                    "human_access_verified": document.human_access_verified,
                    "human_permission": document.human_permission,
                }
                for document in documents
            ]
            (Path(final_dir) / "feishu_docs.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._persist_event(
                session.id,
                InterestEvent(
                    "FEISHU_DOCS_UPLOADED",
                    session.duration_ms,
                    self.state_machine.state,
                    0.0,
                    payload={
                        "documents": payload,
                        "human_delivery_verified": bool(payload)
                        and all(
                            bool(document.get("human_access_verified"))
                            for document in payload
                        ),
                    },
                ),
                result,
            )
            self.record_stage(
                result,
                "feishu_delivery",
                "SUCCEEDED",
                document_count=len(payload),
                human_delivery_verified=bool(payload)
                and all(
                    bool(document.get("human_access_verified")) for document in payload
                ),
            )
        except Exception as exc:
            self._runtime_error(session, session.duration_ms, "FEISHU_DOCS_ERROR", exc, result)
            self.record_stage(
                result,
                "feishu_delivery",
                "FAILED",
                error=type(exc).__name__,
                message=redact_text(exc),
            )

    def _backup_session(self, session: Session, result: SessionResult) -> None:
        if self.backup is None or self.artifacts is None:
            return
        try:
            backup_result = self.backup.backup(self.artifacts.root, session.id)
            (self.artifacts.root / "backup.json").write_text(
                json.dumps(backup_result.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._persist_event(
                session.id,
                InterestEvent(
                    "BACKUP_COMPLETED" if backup_result.status == "uploaded" else "BACKUP_UNAVAILABLE",
                    session.duration_ms,
                    self.state_machine.state,
                    0.0,
                    payload=backup_result.__dict__,
                ),
                result,
            )
        except Exception as exc:
            self._runtime_error(session, session.duration_ms, "BACKUP_ERROR", exc, result)

    def _send_session_summary(self, session: Session, result: SessionResult) -> None:
        notifier = self.summary_notifier
        if notifier is None:
            return
        completed = session.status is SessionStatus.COMPLETED
        lines = [
            (
                "✅ Live Sentinel 直播归档完成"
                if completed
                else "⚠️ Live Sentinel 采集完成，但主归档尚未发布"
            ),
            f"主播：{session.up_name}",
            f"Session：{session.id}",
            f"时长：{session.duration_ms / 1000:.1f}s",
            f"重点片段：{len(result.highlights)}",
        ]
        if session.status is SessionStatus.RETAINED_IN_STAGING and self.artifacts is not None:
            lines.extend(
                [
                    "状态：RETAINED_IN_STAGING",
                    f"暂存位置：{self.artifacts.root}",
                    "需要检查 T5 挂载/空间后重新执行校验发布。",
                ]
            )
        if session.final_audio_path:
            lines.append(f"主音频：{Path(session.final_audio_path).name}")
        if self.offline_finalizer is not None:
            final_dir = Path(getattr(self.offline_finalizer, "output_dir", ""))
            docs_file = final_dir / "feishu_docs.json"
            if docs_file.exists():
                try:
                    documents = json.loads(docs_file.read_text(encoding="utf-8"))
                    human_verified = bool(documents) and all(
                        isinstance(document, dict)
                        and bool(document.get("human_access_verified"))
                        for document in documents
                    )
                    lines.append(
                        "飞书交付：人类访问已验证"
                        if human_verified
                        else "飞书交付：仅已创建，尚未验证人类访问"
                    )
                    for document in documents:
                        if isinstance(document, dict):
                            url = document.get("url") or document.get("document_id")
                            if url:
                                lines.append(f"飞书文档：{url}")
                except (OSError, json.JSONDecodeError):
                    lines.append("飞书文档：已上传（索引读取失败，请查看归档目录）")
        if self.artifacts is not None:
            backup_file = self.artifacts.root / "backup.json"
            if backup_file.exists():
                try:
                    backup = json.loads(backup_file.read_text(encoding="utf-8"))
                    lines.append(f"异地备份：{backup.get('status', 'unknown')}")
                except (OSError, json.JSONDecodeError):
                    pass
        try:
            send_text = getattr(notifier, "send_text", None)
            if callable(send_text):
                send_text("\n".join(lines))
            else:
                raise RuntimeError("摘要通知器不支持 send_text")
        except Exception as exc:
            self._runtime_error(session, session.duration_ms, "SUMMARY_NOTIFICATION_ERROR", exc, result)

    def _write_session_state(self, session: Session) -> None:
        if self.store is not None:
            self.store.update_session(session)
        if self.artifacts is not None:
            self.artifacts.write_session(session)

    def complete_delivery(self, result: SessionResult) -> SessionResult:
        """在主归档已验证发布后执行外部交付并提交 COMPLETED。"""

        session = result.session
        self._upload_cloud_documents(session, result)
        session.status = SessionStatus.COMPLETED
        self._write_session_state(session)
        if self.offline_finalizer is not None and result.final_audio is not None:
            refresh_metadata = getattr(self.offline_finalizer, "refresh_session_metadata", None)
            if callable(refresh_metadata):
                refresh_metadata(session)
        # 完成摘要只承诺已验证的主归档；可选异地备份不能延迟或改变该承诺。
        self._send_session_summary(session, result)
        self._backup_session(session, result)
        return result

    def retain_in_staging(
        self,
        result: SessionResult,
        error: Exception,
    ) -> SessionResult:
        """主归档发布失败时保留唯一副本并发送准确的降级通知。"""

        session = result.session
        session.status = SessionStatus.RETAINED_IN_STAGING
        self._persist_event(
            session.id,
            InterestEvent(
                "PROMOTION_ERROR",
                session.duration_ms,
                self.state_machine.state,
                0.0,
                payload={"error": type(error).__name__, "message": str(error)},
            ),
            result,
        )
        self._write_session_state(session)
        if self.offline_finalizer is not None and result.final_audio is not None:
            refresh_metadata = getattr(self.offline_finalizer, "refresh_session_metadata", None)
            if callable(refresh_metadata):
                refresh_metadata(session)
        self._send_session_summary(session, result)
        return result

    def run(self, session: Session) -> SessionResult:
        """运行并完成一场 Session；P0 归档失败会使 Session 进入 FAILED。"""

        result = SessionResult(session=session)
        self.state_machine = self.state_machine or InterestStateMachine(
            session.id, self.config.interest
        )
        if self.resume_checkpoint is not None:
            runtime = self.resume_checkpoint.runtime_state or {}
            if "interest" in runtime:
                self.state_machine.restore(runtime["interest"])
            if hasattr(self.analyzer, "_last_topic"):
                self.analyzer._last_topic = str(runtime.get("last_topic", ""))
                self.analyzer._last_text = str(runtime.get("last_text", ""))
            if self.store is not None:
                previous_transcripts = self.store.load_realtime_transcripts(session.id)
                self.transcript_buffer.extend(previous_transcripts)
                result.realtime_transcripts.extend(previous_transcripts)
        if self.store is not None:
            self.store.create_session(session)
        if self.artifacts is not None:
            self.artifacts.write_session(session)
        session.status = SessionStatus.INITIALIZING
        if self.store is not None:
            self.store.update_session(session)
        if self.artifacts is not None:
            self.artifacts.write_session(session)
        last_frame_end_ms = (
            self.resume_checkpoint.last_timestamp_ms if self.resume_checkpoint else 0
        )
        last_checkpoint_ms = last_frame_end_ms
        next_analysis_ms: int | None = None
        resume_gap_recorded = False
        last_analysis_ms: int | None = (
            (self.resume_checkpoint.runtime_state or {}).get("last_analysis_ms")
            if self.resume_checkpoint else None
        )
        archive_closed = False
        recording_stage_done = False
        realtime_stage_started = False
        realtime_stage_done = False
        offline_stage_done = False
        summary_stage_done = False
        realtime_stop = threading.Event()
        realtime_thread: threading.Thread | None = None
        analysis_stop = threading.Event()
        analysis_tasks: Queue[int] = Queue(maxsize=2)
        analysis_thread: threading.Thread | None = None
        try:
            self.record_stage(result, "recording", "STARTED")
            # Local Offline ASR can be scoped to the lifetime of this task. In the
            # default lazy mode this is a no-op and the service starts only when
            # post-processing actually begins.
            if self.offline_finalizer is not None:
                try:
                    start_for_session = getattr(
                        self.offline_finalizer, "start_for_session", None
                    )
                    if callable(start_for_session):
                        start_for_session()
                except Exception as exc:
                    self._runtime_error(
                        session,
                        0,
                        "OFFLINE_ASR_START_ERROR",
                        exc,
                        result,
                    )
            self.audio_source.start()
            # Startup adapters may have emitted mode/probe events before the first
            # frame. Persist them immediately so a first-read failure is auditable.
            self._drain_capture_audit(session, result)
            self.archive_writer.start()
            session.status = SessionStatus.RUNNING
            if self.store is not None:
                self.store.update_session(session)
            if self.artifacts is not None:
                self.artifacts.write_session(session)
            if self.resume_checkpoint is not None:
                self._persist_event(
                    session.id,
                    InterestEvent(
                        "SESSION_RESUMED", last_frame_end_ms,
                        self.state_machine.state, 0.0,
                        payload={
                            "at": self._now_iso(),
                            "previous_end_ms": self.resume_checkpoint.last_timestamp_ms,
                        },
                    ),
                    result,
                )
            if self.realtime_enabled:
                self.record_stage(result, "realtime_asr", "STARTED")
                realtime_stage_started = True
                realtime_thread = threading.Thread(
                    target=self._run_realtime_worker,
                    args=(session, result, realtime_stop),
                    name=f"live-sentinel-realtime-{session.id}",
                    daemon=True,
                )
                realtime_thread.start()
                analysis_thread = threading.Thread(
                    target=self._run_analysis_worker,
                    args=(session, result, analysis_tasks, analysis_stop),
                    name=f"live-sentinel-analysis-{session.id}",
                    daemon=True,
                )
                analysis_thread.start()
            else:
                self.record_stage(result, "realtime_asr", "SKIPPED", reason="disabled")
                realtime_stage_done = True
            interval_ms = self.config.analysis.interval_sec * 1000
            while True:
                try:
                    frame = self.audio_source.read()
                finally:
                    self._drain_capture_audit(session, result)
                if frame is None:
                    break
                if self.resume_checkpoint is not None and not resume_gap_recorded:
                    resume_gap_recorded = True
                    if frame.timestamp_ms > self.resume_checkpoint.last_timestamp_ms:
                        self._persist_event(
                            session.id,
                            InterestEvent(
                                "CAPTURE_GAP",
                                frame.timestamp_ms,
                                self.state_machine.state,
                                0.0,
                                payload={
                                    "start_ms": self.resume_checkpoint.last_timestamp_ms,
                                    "end_ms": frame.timestamp_ms,
                                    "duration_ms": (
                                        frame.timestamp_ms
                                        - self.resume_checkpoint.last_timestamp_ms
                                    ),
                                    "reason": "service_restart",
                                },
                            ),
                            result,
                        )
                last_frame_end_ms = max(last_frame_end_ms, frame.end_ms)
                # 先发布再消费，保证生产架构和真实异步适配器一致。
                self.tee.publish(frame, include_realtime=self.realtime_enabled)
                archive_frame = self.tee.get_archive(timeout=1.0)
                if archive_frame is None:
                    raise RuntimeError("归档队列异常：音频帧未取出")
                self.archive_writer.write(archive_frame)
                if (
                    self.checkpoint_store is not None
                    and frame.end_ms - last_checkpoint_ms >= self.checkpoint_interval_ms
                ):
                    self.checkpoint_store.save(
                        Checkpoint(session.id, frame.end_ms).with_timestamp(frame.end_ms)
                    )
                    last_checkpoint_ms = frame.end_ms
                if self.realtime_enabled:
                    if next_analysis_ms is None:
                        # A resumed capture has a real wall-time gap. Do not
                        # manufacture analysis windows for the missing audio.
                        next_analysis_ms = (
                            frame.end_ms + interval_ms
                            if self.resume_checkpoint else interval_ms
                        )
                    while next_analysis_ms is not None and frame.end_ms >= next_analysis_ms:
                        self._enqueue_latest_analysis(
                            session,
                            next_analysis_ms,
                            result,
                            analysis_tasks,
                        )
                        last_analysis_ms = next_analysis_ms
                        next_analysis_ms += interval_ms
            session.duration_ms = last_frame_end_ms
            realtime_stop.set()
            if realtime_thread is not None:
                realtime_thread.join(timeout=self.realtime_join_timeout_sec)
                if realtime_thread.is_alive():
                    self._runtime_error(
                        session,
                        last_frame_end_ms,
                        "REALTIME_TIMEOUT",
                        TimeoutError("实时链路未在结束等待窗口内完成"),
                        result,
                    )
                    # Provider calls have their own bounded timeout. Once capture
                    # has ended, wait for the worker to stop mutating stores before
                    # finalization closes/reopens SQLite during T5 promotion.
                    realtime_thread.join()
                self.record_stage(
                    result,
                    "realtime_asr",
                    "SUCCEEDED",
                    transcript_count=len(result.realtime_transcripts),
                )
                realtime_stage_done = True
            if (
                self.realtime_enabled
                and last_frame_end_ms
                and last_analysis_ms != last_frame_end_ms
            ):
                self._enqueue_latest_analysis(
                    session,
                    last_frame_end_ms,
                    result,
                    analysis_tasks,
                )
                last_analysis_ms = last_frame_end_ms
            analysis_stop.set()
            if analysis_thread is not None:
                # LLM/notification are allowed to delay post-processing, but they
                # can no longer delay or corrupt the already completed capture.
                analysis_thread.join()
            if self.stop_event is not None and self.stop_event.is_set():
                self.audio_source.stop()
                segments = self.archive_writer.close()
                archive_closed = True
                if self.store is not None:
                    for segment in segments:
                        self.store.add_audio_segment(session.id, segment)
                paused_at = self._now_iso()
                if self.checkpoint_store is not None:
                    runtime_state = {
                        "interest": self.state_machine.snapshot(),
                        "last_topic": getattr(self.analyzer, "_last_topic", ""),
                        "last_text": getattr(self.analyzer, "_last_text", ""),
                        "last_analysis_ms": last_analysis_ms,
                    }
                    self.checkpoint_store.save(Checkpoint(
                        session.id, last_frame_end_ms,
                        segments[-1].id if segments else None,
                        updated_at=paused_at, paused_at=paused_at,
                        runtime_state=runtime_state,
                    ))
                session.status = SessionStatus.PAUSED
                session.duration_ms = last_frame_end_ms
                session.end_time = None
                self._persist_event(
                    session.id,
                    InterestEvent(
                        "SESSION_PAUSED", last_frame_end_ms,
                        self.state_machine.state, 0.0,
                        payload={"at": paused_at, "segment_count": len(segments)},
                    ),
                    result,
                )
                self._write_session_state(session)
                result.highlights = list(self.state_machine.highlights)
                return result
            session.status = SessionStatus.STOPPING
            session.duration_ms = last_frame_end_ms
            end_event = self.state_machine.close(last_frame_end_ms)
            if end_event is not None:
                self._persist_event(session.id, end_event, result)
                if self.store is not None:
                    for highlight in self.state_machine.highlights:
                        self.store.upsert_highlight(highlight)
            session.end_time = self._now_iso()
            if self.store is not None:
                self.store.update_session(session)
            if self.artifacts is not None:
                self.artifacts.write_session(session)
            self.audio_source.stop()
            segments = self.archive_writer.close()
            archive_closed = True
            if self.checkpoint_store is not None:
                last_segment_id = segments[-1].id if segments else None
                self.checkpoint_store.save(
                    Checkpoint(session.id, last_frame_end_ms, last_segment_id).with_timestamp(
                        last_frame_end_ms, last_segment_id
                    )
                )
            if self.store is not None:
                for segment in segments:
                    self.store.add_audio_segment(session.id, segment)
            self.record_stage(
                result,
                "recording",
                "SUCCEEDED",
                duration_ms=session.duration_ms,
                segment_count=len(segments),
            )
            recording_stage_done = True
            if self.finalizer is not None and segments and self.config.audio.merge_after_session:
                session.status = SessionStatus.AUDIO_FINALIZING
                if self.store is not None:
                    self.store.update_session(session)
                if self.artifacts is not None:
                    self.artifacts.write_session(session)
                result.final_audio = self.finalizer.finalize(
                    session.id,
                    segments,
                    session_duration_ms=session.duration_ms,
                    gaps=[
                        TimelineGap(start, end)
                        for start, end in getattr(self.archive_writer, "gaps", [])
                    ],
                )
                session.final_audio_path = str(result.final_audio.file_path)
                if self.store is not None:
                    self.store.add_audio_final(
                        result.final_audio,
                        sample_rate=self.config.audio.archive_sample_rate,
                        channels=self.config.audio.archive_channels,
                    )
            if self.offline_finalizer is not None and result.final_audio is not None:
                session.status = SessionStatus.POST_PROCESSING
                if self.store is not None:
                    self.store.update_session(session)
                if self.artifacts is not None:
                    self.artifacts.write_session(session)
                self.record_stage(result, "offline_asr", "STARTED")
                self.record_stage(result, "summary", "STARTED")
                try:
                    self.offline_finalizer.run(
                        session=session,
                        final_audio=result.final_audio,
                        realtime_transcripts=result.realtime_transcripts,
                        highlights=self.state_machine.highlights,
                    )
                    # OfflineFinalizer keeps the exact segments it wrote. Persist
                    # them in SQLite when a store is configured so the database and
                    # files describe the same final transcript.
                    final_transcripts = getattr(self.offline_finalizer, "last_transcripts", [])
                    if self.store is not None:
                        replace_transcripts = getattr(
                            self.store, "replace_final_transcripts", None
                        )
                        if callable(replace_transcripts):
                            replace_transcripts(session.id, list(final_transcripts))
                        else:
                            for transcript in final_transcripts:
                                self.store.add_final_transcript(
                                    session.id,
                                    transcript,
                                    transcript.text,
                                )
                        replace_chapters = getattr(self.store, "replace_chapters", None)
                        if callable(replace_chapters):
                            replace_chapters(
                                session.id,
                                list(getattr(self.offline_finalizer, "last_chapters", [])),
                            )
                    self.record_stage(
                        result,
                        "offline_asr",
                        "SUCCEEDED",
                        transcript_count=len(final_transcripts),
                        source=getattr(
                            self.offline_finalizer,
                            "transcript_source",
                            "unknown",
                        ),
                    )
                    offline_stage_done = True
                    self.record_stage(
                        result,
                        "summary",
                        "SUCCEEDED",
                        chapter_count=len(
                            getattr(self.offline_finalizer, "last_chapters", [])
                        ),
                        highlight_count=len(self.state_machine.highlights),
                    )
                    summary_stage_done = True
                except Exception as exc:
                    # The final audio is already durable at this point. A provider
                    # outage must not turn a successful P0 archive into a failed
                    # session; leave an auditable error and keep the audio usable.
                    self._runtime_error(
                        session,
                        session.duration_ms,
                        "OFFLINE_ASR_ERROR",
                        exc,
                        result,
                    )
                    self.record_stage(
                        result,
                        "offline_asr",
                        "FAILED",
                        error=type(exc).__name__,
                        message=redact_text(exc),
                    )
                    offline_stage_done = True
                    self.record_stage(
                        result,
                        "summary",
                        "FAILED",
                        error=type(exc).__name__,
                        message=redact_text(exc),
                    )
                    summary_stage_done = True
            else:
                reason = "no_final_audio" if result.final_audio is None else "disabled"
                self.record_stage(result, "offline_asr", "SKIPPED", reason=reason)
                self.record_stage(result, "summary", "SKIPPED", reason=reason)
                offline_stage_done = True
                summary_stage_done = True
            result.highlights = list(self.state_machine.highlights)
            if self.defer_delivery:
                # The runner must close SQLite and complete verified staging -> T5
                # promotion before any external document or success notification.
                session.status = SessionStatus.DELIVERY_PENDING
                self._write_session_state(session)
                if self.offline_finalizer is not None and result.final_audio is not None:
                    refresh_metadata = getattr(
                        self.offline_finalizer, "refresh_session_metadata", None
                    )
                    if callable(refresh_metadata):
                        refresh_metadata(session)
                return result
            return self.complete_delivery(result)
        except Exception as exc:
            # Do not lose startup/capture events when the failure occurs before the
            # first frame. Audit failures must never mask the original exception.
            try:
                self._drain_capture_audit(session, result)
                if realtime_stage_started and not realtime_stage_done:
                    self.record_stage(
                        result,
                        "realtime_asr",
                        "FAILED",
                        error=type(exc).__name__,
                        message=redact_text(exc),
                    )
                if not recording_stage_done:
                    self.record_stage(
                        result,
                        "recording",
                        "FAILED",
                        error=type(exc).__name__,
                        message=redact_text(exc),
                    )
                if not offline_stage_done and session.status is SessionStatus.POST_PROCESSING:
                    self.record_stage(
                        result,
                        "offline_asr",
                        "FAILED",
                        error=type(exc).__name__,
                        message=redact_text(exc),
                    )
                if not summary_stage_done and session.status is SessionStatus.POST_PROCESSING:
                    self.record_stage(
                        result,
                        "summary",
                        "FAILED",
                        error=type(exc).__name__,
                        message=redact_text(exc),
                    )
            except Exception:
                pass
            session.status = SessionStatus.FAILED
            session.end_time = self._now_iso()
            session.duration_ms = last_frame_end_ms
            if self.store is not None:
                self.store.update_session(session)
            if self.artifacts is not None:
                self.artifacts.write_session(session)
            raise
        finally:
            try:
                self.audio_source.stop()
            finally:
                try:
                    if not archive_closed:
                        self.archive_writer.close()
                finally:
                    try:
                        close_for_session = getattr(
                            self.offline_finalizer, "close_for_session", None
                        )
                        if callable(close_for_session):
                            close_for_session()
                    except Exception:
                        # A broken local ASR process must not mask the original
                        # archive/finalization result.
                        pass
                    try:
                        close_asr = getattr(self.realtime_asr, "close", None)
                        if callable(close_asr):
                            close_asr()
                    except Exception:
                        # Closing a broken external ASR socket must not mask the
                        # original archive/finalization result.
                        pass
