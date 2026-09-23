"""把已发现的 B 站直播交给现有 SessionManager 执行。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import os
import shutil
import threading
from urllib.parse import urlsplit

from .analysis.analyzer import RuleBasedContentAnalyzer
from .audio.archive import SegmentArchiveWriter
from .audio.finalizer import AudioFinalizer
from .audio.route import MacOSAudioOutputRoute
from .audio.resample import RealtimeAudioPreprocessor
from .audio.source import (
    CaptureAuditBuffer,
    DurationLimitedAudioSource,
    EmptyAudioSource,
    FFmpegAudioSource,
    NonSilenceProbeAudioSource,
    OffsetAudioSource,
    PreStartHookAudioSource,
    RoomAwareAudioSource,
    RoomStatusSnapshot,
    StartHookAudioSource,
    StoppableAudioSource,
)
from .asr.vad import EnergyVAD
from .asr.realtime import NullStreamingASR
from .bilibili import BilibiliRoomInfo, USER_AGENT, fetch_room_info
from .browser_playback import BrowserPlaybackController
from .config import AppConfig
from .integrations import IntegrationBundle, build_integrations
from .models import Session, SessionStatus
from .observability import redact_text
from .postprocess.pipeline import OfflineFinalizer
from .session.checkpoint import CheckpointStore
from .session.manager import SessionManager, SessionResult
from .storage.artifacts import SessionArtifacts
from .storage.sqlite import SQLiteStore
from .storage.promotion import promote_session


LOGGER = logging.getLogger("live_sentinel.runner")


@dataclass(frozen=True)
class BilibiliRunResult:
    root: Path
    result: SessionResult


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _mounted_volume_for(path: Path) -> Path | None:
    """返回路径所属挂载点；尚不存在的尾部目录从最近祖先开始判断。"""

    candidate = _nearest_existing_path(path)
    while candidate != candidate.parent and not candidate.is_mount():
        candidate = candidate.parent
    return candidate if candidate.is_mount() else None


def validate_archive_root(config: AppConfig, output_dir: str | Path | None = None) -> Path:
    archive_root = Path(output_dir or config.storage.root_dir).expanduser().resolve()
    if config.storage.require_mounted_path:
        mount_point = _mounted_volume_for(archive_root)
        system_root = Path(archive_root.anchor)
        if mount_point is None or mount_point == system_root:
            raise RuntimeError(
                f"归档目录要求位于独立挂载卷，但当前路径落在系统卷: {archive_root}"
            )
    archive_root.mkdir(parents=True, exist_ok=True)
    free_gib = shutil.disk_usage(archive_root).free / (1024**3)
    if free_gib < config.storage.minimum_free_gib:
        raise RuntimeError(
            f"归档目录可用空间不足: {free_gib:.1f} GiB < {config.storage.minimum_free_gib:.1f} GiB"
        )
    return archive_root


def _validate_staging_root(config: AppConfig) -> Path | None:
    if not config.storage.staging_dir:
        return None
    staging_root = Path(config.storage.staging_dir).expanduser().resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    free_gib = shutil.disk_usage(staging_root).free / (1024**3)
    if free_gib < config.storage.staging_minimum_free_gib:
        raise RuntimeError(
            "staging 可用空间不足: "
            f"{free_gib:.1f} GiB < {config.storage.staging_minimum_free_gib:.1f} GiB"
        )
    return staging_root


def _browser_audio_url_from_env() -> str | None:
    """Return a loopback-only decoded-audio bridge URL, when configured."""

    raw = os.environ.get("BILIBILI_DRM_AUDIO_URL", "").strip()
    if not raw:
        return None
    if "\r" in raw or "\n" in raw:
        raise RuntimeError("BILIBILI_DRM_AUDIO_URL 格式无效")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path
    ):
        raise RuntimeError("BILIBILI_DRM_AUDIO_URL 仅允许无凭据的本机回环 HTTP 地址")
    return raw


def _playback_output_device(capture_device: str, configured_device: str) -> str:
    """Resolve a system playback route independently from the capture input."""

    device = configured_device.strip() or capture_device
    if not device or "\r" in device or "\n" in device:
        raise RuntimeError("bilibili_capture.playback_output_device 格式无效")
    return device


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"环境变量 {name} 必须是 true/false")


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None or not raw.strip() else int(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是正整数") from exc
    if value <= 0:
        raise RuntimeError(f"环境变量 {name} 必须是正整数")
    return value


def _env_nonnegative_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None or not raw.strip() else float(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是非负数") from exc
    if value < 0:
        raise RuntimeError(f"环境变量 {name} 必须是非负数")
    return value


def _browser_capture_required(info: BilibiliRoomInfo, config: AppConfig) -> bool:
    forced_rooms = {
        str(room_id).strip()
        for room_id in config.bilibili_capture.browser_audio_room_ids
        if str(room_id).strip()
    }
    return info.stream_drm or str(info.room_id) in forced_rooms


def _bilibili_room_status_probe(
    room_id: int,
    *,
    timeout: int,
) -> RoomStatusSnapshot:
    """获取独立的直播状态证据，并尽力识别公开/受保护流切换。"""

    try:
        observed = fetch_room_info(room_id, include_stream=True, timeout=timeout)
    except Exception as stream_error:
        observed = fetch_room_info(room_id, include_stream=False, timeout=timeout)
        if not observed.is_live:
            return RoomStatusSnapshot(False, "offline")
        return RoomStatusSnapshot(
            True,
            "protected_or_unavailable",
            f"{type(stream_error).__name__}: {stream_error}",
        )
    if not observed.is_live:
        return RoomStatusSnapshot(False, "offline")
    return RoomStatusSnapshot(
        True,
        "protected" if observed.stream_drm else "public",
    )


def _rebind_delivery_paths(
    manager: SessionManager,
    root: Path,
    store: SQLiteStore,
) -> None:
    """让延迟交付阶段只写已经发布的根目录和重新打开的数据库。"""

    manager.store = store
    manager.artifacts = SessionArtifacts(root)
    if manager.offline_finalizer is not None:
        manager.offline_finalizer.output_dir = root / "final"
    if manager.finalizer is not None:
        manager.finalizer.output_dir = root / "audio" / "final"


def _finish_staged_delivery(
    *,
    manager: SessionManager,
    result: SessionResult,
    staging_root: Path,
    configured_archive_root: Path,
    config: AppConfig,
    output_dir: str | Path | None = None,
) -> Path:
    """发布 staging 并提交交付；失败时保留 staging 和准确终态。"""

    session_id = result.session.id
    destination = configured_archive_root / session_id
    stage_store = SQLiteStore(staging_root / "session.sqlite")
    try:
        _rebind_delivery_paths(manager, staging_root, stage_store)
        manager.record_stage(result, "t5_promotion", "STARTED")
    finally:
        stage_store.close()
    try:
        archive_root = validate_archive_root(config, output_dir)
        destination = archive_root / session_id
        promote_session(
            staging_root,
            destination,
            verify_sha256=config.storage.verify_promotion_sha256,
        )
        if result.session.final_audio_path:
            result.session.final_audio_path = result.session.final_audio_path.replace(
                str(staging_root), str(destination), 1
            )
        if result.final_audio is not None:
            result.final_audio = replace(
                result.final_audio,
                file_path=Path(
                    str(result.final_audio.file_path).replace(
                        str(staging_root), str(destination), 1
                    )
                ),
            )
    except Exception as exc:
        failure = {
            "status": "retained_in_staging",
            "staging_root": str(staging_root),
            "intended_archive_root": str(destination),
            "error": f"{type(exc).__name__}: {redact_text(exc)}",
        }
        error_path = staging_root / "promotion_error.json"
        error_path.write_text(
            json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        error_path.chmod(0o600)
        LOGGER.exception("Session %s 保留在 staging，提升到主归档失败", session_id)
        delivery_store = SQLiteStore(staging_root / "session.sqlite")
        try:
            _rebind_delivery_paths(manager, staging_root, delivery_store)
            manager.record_stage(
                result,
                "t5_promotion",
                "FAILED",
                error=type(exc).__name__,
                message=redact_text(exc),
            )
            manager.retain_in_staging(result, exc)
        finally:
            delivery_store.close()
        return staging_root

    delivery_store = SQLiteStore(destination / "session.sqlite")
    try:
        _rebind_delivery_paths(manager, destination, delivery_store)
        manager.record_stage(
            result,
            "t5_promotion",
            "SUCCEEDED",
            destination=str(destination),
        )
        manager.complete_delivery(result)
    finally:
        delivery_store.close()
    return destination


def run_bilibili_session(
    info: BilibiliRoomInfo,
    config: AppConfig,
    *,
    output_dir: str | Path | None = None,
    max_duration_sec: int | None = None,
    strict_integrations: bool = False,
    integrations: IntegrationBundle | None = None,
    session_id: str | None = None,
    stop_event: threading.Event | None = None,
    activate_browser: bool = True,
    browser_started_callback: Callable[[], None] | None = None,
) -> BilibiliRunResult:
    browser_capture = _browser_capture_required(info, config)
    if max_duration_sec is not None and max_duration_sec <= 0:
        raise ValueError("max_duration_sec 必须为正数")

    configured_archive_root = Path(output_dir or config.storage.root_dir).expanduser().resolve()
    staging_root = _validate_staging_root(config)
    # With a local staging tier, T5 is not on the capture critical path. Its
    # mount/free-space check is repeated immediately before promotion. Without
    # staging, validate it now because recording writes there directly.
    archive_root = (
        configured_archive_root
        if staging_root is not None
        else validate_archive_root(config, output_dir)
    )
    session_id = session_id or datetime.now(timezone.utc).strftime(
        f"%Y%m%d_%H%M%S_%f_room{info.room_id}"
    )
    root = (staging_root or archive_root) / session_id
    session_root_existed = root.exists()
    checkpoint_store = CheckpointStore(root / "checkpoint.json")
    resume_checkpoint = checkpoint_store.load() if session_root_existed else None
    if session_root_existed and (
        resume_checkpoint is None
        or resume_checkpoint.session_id != session_id
        or not resume_checkpoint.paused_at
    ):
        raise RuntimeError(f"已有 Session 目录但没有可恢复的暂停检查点: {root}")
    if not info.is_live and resume_checkpoint is None:
        raise RuntimeError("房间当前不在线，且没有待收尾的 Session")
    if info.is_live and not browser_capture and not info.stream_url:
        raise RuntimeError("房间当前没有可用播放地址")
    adapters = integrations or build_integrations(config, strict=strict_integrations)
    audio_route: MacOSAudioOutputRoute | None = None
    browser_playback: BrowserPlaybackController | None = None
    capture_audit: CaptureAuditBuffer | None = None
    forced_browser_capture = (
        str(info.room_id)
        in {
            str(room_id).strip()
            for room_id in config.bilibili_capture.browser_audio_room_ids
            if str(room_id).strip()
        }
    )
    if not info.is_live:
        source = EmptyAudioSource()
    elif browser_capture:
        browser_audio_url = _browser_audio_url_from_env()
        if browser_audio_url and not forced_browser_capture:
            source = FFmpegAudioSource(
                browser_audio_url,
                sample_rate=config.audio.archive_sample_rate,
                channels=config.audio.archive_channels,
                frame_ms=100,
            )
        else:
            audio_device = os.environ.get("BILIBILI_DRM_AUDIO_DEVICE", "").strip()
            if not audio_device or "\r" in audio_device or "\n" in audio_device:
                raise RuntimeError(
                    "B 站直播流受 DRM 保护；请配置 BILIBILI_DRM_AUDIO_URL 或 "
                    "BILIBILI_DRM_AUDIO_DEVICE，通过已授权播放器回采录制"
                )
            source = FFmpegAudioSource(
                f":{audio_device}",
                sample_rate=config.audio.archive_sample_rate,
                channels=config.audio.archive_channels,
                frame_ms=100,
                input_format="avfoundation",
            )
            source = NonSilenceProbeAudioSource(
                source,
                probe_window_ms=_env_positive_int(
                    "BILIBILI_DRM_AUDIO_PROBE_MS", 20_000
                ),
                rms_threshold=_env_nonnegative_float(
                    "BILIBILI_DRM_AUDIO_RMS_THRESHOLD", 100.0
                ),
                required_non_silent_ms=_env_positive_int(
                    "BILIBILI_DRM_REQUIRED_SOUND_MS", 500
                ),
            )
            playback_output_device = _playback_output_device(
                audio_device,
                config.bilibili_capture.playback_output_device,
            )
            audio_route = MacOSAudioOutputRoute(playback_output_device)
            open_browser = _env_bool("BILIBILI_DRM_OPEN_BROWSER", True)
            if forced_browser_capture and not open_browser:
                raise RuntimeError(
                    "房间专用策略要求已登录浏览器播放，"
                    "但 BILIBILI_DRM_OPEN_BROWSER=false"
                )
            if open_browser and activate_browser:
                browser_playback = BrowserPlaybackController(
                    app_name=os.environ.get(
                        "BILIBILI_DRM_BROWSER_APP", "Microsoft Edge"
                    ).strip(),
                    url=info.url,
                    room_ids=(str(info.room_id), str(info.requested_room_id)),
                    background=_env_bool("BILIBILI_DRM_BROWSER_BACKGROUND", False),
                )
    else:
        source = FFmpegAudioSource(
            info.stream_url,
            sample_rate=config.audio.archive_sample_rate,
            channels=config.audio.archive_channels,
            frame_ms=100,
            user_agent=USER_AGENT,
            headers={
                "Referer": "https://live.bilibili.com/",
                "Origin": "https://live.bilibili.com",
            },
        )
    audio_source = source
    if browser_playback is not None:
        # Listen on BlackHole before opening the page so the opening audio cannot
        # race ahead of FFmpeg startup. The probe still withholds all frames until
        # enough real sound has been observed.
        def start_browser_playback() -> None:
            browser_playback.start()
            if browser_started_callback is not None:
                browser_started_callback()

        audio_source = StartHookAudioSource(audio_source, start_browser_playback)
    if audio_route is not None:
        # Route system playback inside the Session lifecycle so even a routing
        # failure is persisted as a pre-first-frame recording failure. Capture
        # can remain on BlackHole while playback uses a Multi-Output Device.
        audio_source = PreStartHookAudioSource(audio_source, audio_route.activate)
    if max_duration_sec is not None:
        audio_source = DurationLimitedAudioSource(
            audio_source,
            max_duration_sec * 1000,
        )
    if browser_capture and info.is_live:
        capture_audit = CaptureAuditBuffer()
        capture_settings = config.bilibili_capture
        audio_source = RoomAwareAudioSource(
            audio_source,
            lambda: _bilibili_room_status_probe(
                info.room_id,
                timeout=config.watchlist.discovery_timeout_sec,
            ),
            audit=capture_audit,
            initial_access_mode="protected" if info.stream_drm else "public",
            status_poll_interval_sec=capture_settings.status_poll_interval_sec,
            offline_confirmations=capture_settings.offline_confirmations,
            offline_confirmation_interval_sec=(
                capture_settings.offline_confirmation_interval_sec
            ),
            silence_timeout_sec=capture_settings.continuous_silence_timeout_sec,
            silence_rms_threshold=capture_settings.silence_rms_threshold,
        )
    if stop_event is not None:
        # A service stop is an explicit local control action, not an input EOF;
        # keep it outside the room-aware guard so shutdown does not trigger a
        # false "audio ended while live" failure.
        audio_source = StoppableAudioSource(audio_source, stop_event)
    if resume_checkpoint is not None and info.is_live:
        audio_source = OffsetAudioSource(
            audio_source,
            resume_checkpoint.last_timestamp_ms,
            resume_checkpoint.paused_at,
        )
    writer = SegmentArchiveWriter(
        root / "audio" / "working",
        session_id,
        segment_minutes=config.audio.segment_minutes,
        sample_rate=config.audio.archive_sample_rate,
        channels=config.audio.archive_channels,
        sample_width=config.audio.archive_sample_width,
        codec=config.audio.archive_codec,
    )
    store = SQLiteStore(root / "session.sqlite")
    if resume_checkpoint is not None:
        try:
            session = store.load_session(session_id)
            if session is None or session.status is not SessionStatus.PAUSED:
                raise RuntimeError(f"Session 状态不允许接续: {session_id}")
            writer.restore(store.load_audio_segments(session_id))
            if writer.segments and writer.segments[-1].end_ms != resume_checkpoint.last_timestamp_ms:
                raise RuntimeError("检查点与最后分片时间不一致，拒绝覆盖或丢弃音频")
        except Exception:
            store.close()
            raise
    else:
        session = Session(
            id=session_id,
            room_id=str(info.room_id),
            up_name=info.uname,
            start_time=datetime.now(timezone.utc).isoformat(),
        )
    manager = SessionManager(
        config,
        audio_source,
        writer,
        RealtimeAudioPreprocessor(config.asr.realtime.sample_rate),
        EnergyVAD(),
        adapters.realtime_asr,
        RuleBasedContentAnalyzer(
            config.interest.whitelist,
            config.interest.blacklist,
            structure_markers=config.interest.structure_markers,
            density_markers=config.interest.density_markers,
            tuning=config.interest.tuning,
        ),
        finalizer=AudioFinalizer(root / "audio" / "final", codec=config.audio.archive_codec),
        llm_judge=adapters.llm_judge,
        notifier=adapters.notifier,
        summary_notifier=adapters.summary_notifier,
        feishu_docs=adapters.feishu_docs,
        backup=adapters.backup,
        store=store,
        artifacts=SessionArtifacts(root),
        checkpoint_store=checkpoint_store,
        offline_finalizer=OfflineFinalizer(root / "final", offline_asr=adapters.offline_asr),
        realtime_enabled=info.is_live and not isinstance(adapters.realtime_asr, NullStreamingASR),
        defer_delivery=staging_root is not None,
        capture_audit=capture_audit,
        resume_checkpoint=resume_checkpoint,
        stop_event=stop_event,
    )
    try:
        result = manager.run(session)
    finally:
        if audio_route is not None:
            LOGGER.info(
                "System audio output remains routed to %s by policy",
                audio_route.device,
            )
        store.close()

    if staging_root is not None and result.session.status is not SessionStatus.PAUSED:
        root = _finish_staged_delivery(
            manager=manager,
            result=result,
            staging_root=root,
            configured_archive_root=configured_archive_root,
            config=config,
            output_dir=output_dir,
        )
    return BilibiliRunResult(root=root, result=result)
