"""``python -m live_sentinel`` 的本地 mock 入口。"""

from __future__ import annotations

import argparse
from array import array
from datetime import datetime, timezone
from pathlib import Path

from .analysis.analyzer import RuleBasedContentAnalyzer
from .audio.archive import SegmentArchiveWriter
from .audio.finalizer import AudioFinalizer
from .audio.resample import RealtimeAudioPreprocessor
from .audio.source import IterableAudioSource
from .asr.realtime import CallableStreamingASR
from .asr.vad import EnergyVAD
from .config import AnalysisConfig, AppConfig, AudioConfig, InterestConfig, NotificationConfig
from .models import AudioFrame, Session, TranscriptSegment
from .notification.notifier import ConsoleNotifier
from .postprocess.pipeline import OfflineFinalizer
from .session.manager import SessionManager
from .storage.sqlite import SQLiteStore
from .storage.artifacts import SessionArtifacts
from .session.checkpoint import CheckpointStore


def _demo_frames(duration_sec: int, sample_rate: int = 8_000) -> list[AudioFrame]:
    frame_ms = 100
    samples = sample_rate * frame_ms // 1000
    frames: list[AudioFrame] = []
    loud = array("h", [1200] * samples).tobytes()
    quiet = array("h", [0] * samples).tobytes()
    for index in range(duration_sec * 1000 // frame_ms):
        frames.append(
            AudioFrame(index * frame_ms, loud if index % 2 == 0 else quiet, sample_rate, 1, 2)
        )
    return frames


def run_demo(output_dir: str | Path, duration_sec: int) -> int:
    root = Path(output_dir)
    session_id = datetime.now(timezone.utc).strftime("demo_%Y%m%dT%H%M%SZ")
    config = AppConfig(
        audio=AudioConfig(
            archive_sample_rate=8_000,
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
            whitelist=["AI", "Agent"],
            blacklist=["星座", "日常闲聊"],
            candidate_threshold=0.5,
            hot_threshold=0.6,
            leave_hot_threshold=0.2,
            enter_hot_consecutive_windows=2,
            leave_hot_consecutive_windows=2,
            notification_cooldown_sec=600,
        ),
        notification=NotificationConfig(enabled=True, cooldown_minutes=10),
    )
    session_root = root / session_id
    writer = SegmentArchiveWriter(
        session_root / "audio" / "working",
        session_id,
        segment_minutes=config.audio.segment_minutes,
        sample_rate=config.audio.archive_sample_rate,
        channels=config.audio.archive_channels,
        sample_width=config.audio.archive_sample_width,
        codec=config.audio.archive_codec,
    )

    def transcribe(speech):
        return TranscriptSegment(
            speech.start_ms,
            speech.end_ms,
            "AI Agent 第一，核心问题是 Runtime 的职责边界。",
        )

    store = SQLiteStore(session_root / "session.sqlite")
    manager = SessionManager(
        config,
        IterableAudioSource(_demo_frames(duration_sec, config.audio.archive_sample_rate)),
        writer,
        RealtimeAudioPreprocessor(config.asr.realtime.sample_rate),
        EnergyVAD(rms_threshold=100, silence_ms=100),
        CallableStreamingASR(transcribe),
        RuleBasedContentAnalyzer(
            config.interest.whitelist,
            config.interest.blacklist,
            structure_markers=config.interest.structure_markers,
            density_markers=config.interest.density_markers,
            tuning=config.interest.tuning,
        ),
        finalizer=AudioFinalizer(session_root / "audio" / "final", codec="wav"),
        notifier=ConsoleNotifier(),
        store=store,
        offline_finalizer=OfflineFinalizer(session_root / "final"),
        artifacts=SessionArtifacts(session_root),
        checkpoint_store=CheckpointStore(session_root / "checkpoint.json"),
    )
    session = Session(
        id=session_id,
        room_id="mock-room",
        up_name="本地演示",
        start_time=datetime.now(timezone.utc).isoformat(),
    )
    result = manager.run(session)
    store.close()
    print(f"Session: {session.id}")
    print(f"Status: {session.status.value}")
    print(f"Events: {len(result.events)} | Highlights: {len(result.highlights)}")
    if result.final_audio:
        print(f"Final audio: {result.final_audio.file_path}")
    print(f"Artifacts: {session_root / 'final'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m live_sentinel", description="B站直播监听系统 V1 本地演示"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="使用合成 PCM 跑通 Session 闭环")
    demo.add_argument("--output-dir", default="demo-output", help="输出目录")
    demo.add_argument("--duration-sec", type=int, default=8, help="演示时长，默认 8 秒")
    args = parser.parse_args()
    if args.duration_sec <= 0:
        parser.error("--duration-sec 必须为正数")
    if args.command == "demo":
        return run_demo(args.output_dir, args.duration_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
