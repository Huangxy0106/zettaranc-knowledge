"""可替换的音频输入接口。

V1 不假设如何从 B 站获得音频。真实的系统音频、浏览器桥接或其他授权输入
只需实现 ``AudioSource``，核心处理链路不需要改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from array import array
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Empty, Queue
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Iterable

from ..models import AudioFrame


class AudioSource(ABC):
    """一场直播的 PCM 输入。``read`` 返回 None 表示输入结束。"""

    def start(self) -> None:
        return None

    @abstractmethod
    def read(self) -> AudioFrame | None:
        raise NotImplementedError

    def stop(self) -> None:
        return None


class LiveAudioInterruptedError(RuntimeError):
    """房间仍在直播时，授权音频输入已不可信。"""


@dataclass(frozen=True)
class RoomStatusSnapshot:
    is_live: bool
    access_mode: str = "unknown"
    detail: str | None = None


@dataclass(frozen=True)
class CaptureAuditEvent:
    event_type: str
    timestamp_ms: int
    payload: dict[str, Any]


class CaptureAuditBuffer:
    """采集适配器与 Session 审计存储之间的轻量队列。"""

    def __init__(self) -> None:
        self._events: deque[CaptureAuditEvent] = deque()
        self._lock = threading.Lock()

    def emit(self, event_type: str, timestamp_ms: int, **payload: Any) -> None:
        with self._lock:
            self._events.append(
                CaptureAuditEvent(event_type, timestamp_ms, dict(payload))
            )

    def drain(self) -> list[CaptureAuditEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events


class IterableAudioSource(AudioSource):
    """将可迭代的 AudioFrame 作为输入，方便测试和离线回放。"""

    def __init__(self, frames: Iterable[AudioFrame]):
        self._frames = iter(frames)
        self._started = False

    def start(self) -> None:
        self._started = True

    def read(self) -> AudioFrame | None:
        if not self._started:
            raise RuntimeError("AudioSource 尚未 start")
        try:
            return next(self._frames)
        except StopIteration:
            return None


class QueueAudioSource(AudioSource):
    """适用于异步采集适配器的有界队列输入。"""

    _SENTINEL = object()

    def __init__(self, maxsize: int = 32):
        if maxsize <= 0:
            raise ValueError("maxsize 必须为正数")
        self.queue: Queue[AudioFrame | object] = Queue(maxsize=maxsize)
        self._started = False
        self._closed = False

    def start(self) -> None:
        self._started = True

    def put(self, frame: AudioFrame, timeout: float | None = None) -> None:
        if self._closed:
            raise RuntimeError("AudioSource 已关闭")
        self.queue.put(frame, timeout=timeout)

    def close_input(self) -> None:
        if not self._closed:
            self._closed = True
            self.queue.put(self._SENTINEL)

    def read(self, timeout: float | None = None) -> AudioFrame | None:
        if not self._started:
            raise RuntimeError("AudioSource 尚未 start")
        try:
            item = self.queue.get(timeout=timeout)
        except Empty:
            return None
        if item is self._SENTINEL:
            return None
        return item  # type: ignore[return-value]

    def stop(self) -> None:
        self.close_input()


class FFmpegAudioSource(AudioSource):
    """从一个已授权的音频/直播输入读取固定大小的 PCM 帧。

    该适配器只负责把 ``ffmpeg`` 的标准输出变成 ``AudioFrame``，不负责获取或
    绕过 B 站权限、DRM，也不自动控制播放器。输入 URL/命令由调用方负责提供。
    """

    def __init__(
        self,
        input_url: str,
        sample_rate: int = 48_000,
        channels: int = 2,
        frame_ms: int = 100,
        ffmpeg_bin: str = "ffmpeg",
        input_format: str | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        if not input_url:
            raise ValueError("input_url 不能为空")
        if sample_rate <= 0 or channels <= 0 or frame_ms <= 0:
            raise ValueError("音频参数必须为正数")
        self.input_url = input_url
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.ffmpeg_bin = ffmpeg_bin
        self.input_format = input_format
        self.user_agent = user_agent
        self.headers = dict(headers or {})
        self._process: subprocess.Popen[bytes] | None = None
        self._timestamp_ms = 0
        self._bytes_per_frame = sample_rate * frame_ms // 1000 * channels * 2
        self._stderr_tail: deque[bytes] = deque(maxlen=64)
        self._stderr_thread: threading.Thread | None = None

    def _drain_stderr(self, process: subprocess.Popen[bytes]) -> None:
        if process.stderr is None:
            return
        while True:
            chunk = process.stderr.read(4_096)
            if not chunk:
                return
            self._stderr_tail.append(chunk)

    def _error_tail(self) -> str:
        raw = b"".join(self._stderr_tail)[-4_000:]
        return raw.decode("utf-8", errors="replace").strip()

    def start(self) -> None:
        if self._process is not None:
            return
        if shutil.which(self.ffmpeg_bin) is None:
            raise RuntimeError("找不到 ffmpeg，无法读取外部音频输入")
        command = [self.ffmpeg_bin, "-loglevel", "error"]
        if self.user_agent:
            command.extend(["-user_agent", self.user_agent])
        if self.headers:
            header_text = "".join(f"{key}: {value}\r\n" for key, value in self.headers.items())
            command.extend(["-headers", header_text])
        if self.input_format:
            command.extend(["-f", self.input_format])
        command.extend(
            [
                "-i",
                self.input_url,
                "-vn",
                "-f",
                "s16le",
                "-ar",
                str(self.sample_rate),
                "-ac",
                str(self.channels),
                "pipe:1",
            ]
        )
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._timestamp_ms = 0
        self._stderr_tail.clear()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self._process,),
            name="live-sentinel-ffmpeg-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    def read(self) -> AudioFrame | None:
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("FFmpegAudioSource 尚未 start")
        data = self._process.stdout.read(self._bytes_per_frame)
        if not data:
            process = self._process
            try:
                return_code = process.wait(timeout=1.0)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("FFmpeg 音频输出意外关闭，但进程仍在运行") from exc
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=1.0)
            if return_code != 0:
                detail = self._error_tail() or "没有 stderr 详情"
                raise RuntimeError(f"FFmpeg 音频输入异常退出，returncode={return_code}: {detail}")
            return None
        # 读取到的最后一块可能不足一个时间帧，但仍需保留，不能静默丢弃。
        frame = AudioFrame(
            timestamp_ms=self._timestamp_ms,
            pcm=data,
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=2,
        )
        self._timestamp_ms = frame.end_ms
        return frame

    def stop(self) -> None:
        if self._process is None:
            return
        process = self._process
        self._process = None
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        finally:
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=1.0)
                self._stderr_thread = None
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


class NonSilenceProbeAudioSource(AudioSource):
    """先确认输入确有声音，再把缓存帧无损交给归档链路。

    探测期间的帧保存在内存中；一旦累计到足够的非静音时长，会从第一帧开始按原顺序
    返回，因此探针不会截断开场音频。超过探测窗口仍无有效声音会失败关闭，而不是生成
    一场状态为 COMPLETED 的静音 Session。
    """

    def __init__(
        self,
        source: AudioSource,
        *,
        probe_window_ms: int = 20_000,
        rms_threshold: float = 100.0,
        required_non_silent_ms: int = 500,
    ):
        if probe_window_ms <= 0:
            raise ValueError("probe_window_ms 必须为正数")
        if rms_threshold < 0:
            raise ValueError("rms_threshold 不能为负数")
        if required_non_silent_ms <= 0 or required_non_silent_ms > probe_window_ms:
            raise ValueError("required_non_silent_ms 必须在探测窗口内")
        self.source = source
        self.probe_window_ms = probe_window_ms
        self.rms_threshold = rms_threshold
        self.required_non_silent_ms = required_non_silent_ms
        self._buffer: deque[AudioFrame] = deque()
        self._proven = False
        self._observed_ms = 0
        self._non_silent_ms = 0

    @staticmethod
    def _rms(frame: AudioFrame) -> float:
        if frame.sample_width != 2 or not frame.pcm:
            return 0.0
        samples = array("h")
        samples.frombytes(frame.pcm)
        if not samples:
            return 0.0
        total = sum(int(value) * int(value) for value in samples)
        return (total / len(samples)) ** 0.5

    def start(self) -> None:
        self.source.start()

    def read(self) -> AudioFrame | None:
        if self._proven:
            if self._buffer:
                return self._buffer.popleft()
            return self.source.read()

        while not self._proven:
            frame = self.source.read()
            if frame is None:
                return None
            self._buffer.append(frame)
            self._observed_ms += frame.duration_ms
            if self._rms(frame) >= self.rms_threshold:
                self._non_silent_ms += frame.duration_ms
            if self._non_silent_ms >= self.required_non_silent_ms:
                self._proven = True
                return self._buffer.popleft()
            if self._observed_ms >= self.probe_window_ms:
                raise RuntimeError(
                    "音频非静音探测失败："
                    f"{self._observed_ms}ms 内仅检测到 {self._non_silent_ms}ms 有效声音"
                )
        return None

    def stop(self) -> None:
        self.source.stop()


class RoomAwareAudioSource(AudioSource):
    """持续核验房间状态和音频活性，防止将掉流误判为下播。"""

    def __init__(
        self,
        source: AudioSource,
        status_probe: Callable[[], RoomStatusSnapshot],
        *,
        audit: CaptureAuditBuffer,
        initial_access_mode: str,
        status_poll_interval_sec: float = 30,
        offline_confirmations: int = 3,
        offline_confirmation_interval_sec: float = 5,
        silence_timeout_sec: int = 180,
        silence_rms_threshold: float = 100.0,
    ):
        if status_poll_interval_sec <= 0:
            raise ValueError("status_poll_interval_sec 必须为正数")
        if offline_confirmations <= 0:
            raise ValueError("offline_confirmations 必须为正数")
        if offline_confirmation_interval_sec <= 0:
            raise ValueError("offline_confirmation_interval_sec 必须为正数")
        if silence_timeout_sec <= 0:
            raise ValueError("silence_timeout_sec 必须为正数")
        if silence_rms_threshold < 0:
            raise ValueError("silence_rms_threshold 不能为负数")
        self.source = source
        self.status_probe = status_probe
        self.audit = audit
        self.initial_access_mode = initial_access_mode
        self.status_poll_interval_ms = status_poll_interval_sec * 1000
        self.offline_confirmations = offline_confirmations
        self.offline_confirmation_interval_sec = offline_confirmation_interval_sec
        self.silence_timeout_ms = silence_timeout_sec * 1000
        self.silence_rms_threshold = silence_rms_threshold
        self._offline_count = 0
        self._silent_ms = 0
        self._last_frame_end_ms = 0
        self._access_mode = initial_access_mode
        self._started = False
        self._condition = threading.Condition()
        self._monitor_stop = threading.Event()
        self._monitor_wake = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._status_revision = 0
        self._latest_snapshot = RoomStatusSnapshot(True, initial_access_mode)
        self._last_status_error: Exception | None = None
        self._offline_confirmed = False
        self._silence_probe_revision: int | None = None
        self._silence_probe_deadline: float | None = None

    @staticmethod
    def _rms(frame: AudioFrame) -> float:
        return NonSilenceProbeAudioSource._rms(frame)

    def start(self) -> None:
        self.source.start()
        self._started = True
        self.audit.emit(
            "CAPTURE_MODE_SELECTED",
            0,
            capture_mode="browser_system_audio",
            access_mode=self.initial_access_mode,
        )
        self._monitor_thread = threading.Thread(
            target=self._run_status_monitor,
            name="live-sentinel-room-status",
            daemon=True,
        )
        self._monitor_thread.start()

    def _probe_status(self, *, reason: str) -> None:
        with self._condition:
            timestamp_ms = self._last_frame_end_ms
        try:
            snapshot = self.status_probe()
        except Exception as exc:
            self.audit.emit(
                "ROOM_STATUS_PROBE_ERROR",
                timestamp_ms,
                reason=reason,
                error=type(exc).__name__,
                message=str(exc),
            )
            with self._condition:
                self._last_status_error = exc
                self._status_revision += 1
                self._condition.notify_all()
            return
        with self._condition:
            if snapshot.access_mode != self._access_mode:
                self.audit.emit(
                    "STREAM_ACCESS_MODE_CHANGED",
                    timestamp_ms,
                    previous=self._access_mode,
                    current=snapshot.access_mode,
                    detail=snapshot.detail,
                )
                self._access_mode = snapshot.access_mode
            if snapshot.is_live:
                if self._offline_count:
                    self.audit.emit(
                        "ROOM_LIVE_RECONFIRMED",
                        timestamp_ms,
                        previous_offline_observations=self._offline_count,
                    )
                self._offline_count = 0
            else:
                self._offline_count += 1
                self.audit.emit(
                    "ROOM_OFFLINE_OBSERVED",
                    timestamp_ms,
                    confirmation=self._offline_count,
                    required=self.offline_confirmations,
                    reason=reason,
                )
                if (
                    self._offline_count >= self.offline_confirmations
                    and not self._offline_confirmed
                ):
                    self._offline_confirmed = True
                    self.audit.emit(
                        "ROOM_OFFLINE_CONFIRMED",
                        timestamp_ms,
                        confirmations=self._offline_count,
                    )
            self._latest_snapshot = snapshot
            self._last_status_error = None
            self._status_revision += 1
            self._condition.notify_all()

    def _run_status_monitor(self) -> None:
        interval = self.status_poll_interval_ms / 1000
        while not self._monitor_stop.is_set():
            requested = self._monitor_wake.wait(interval)
            self._monitor_wake.clear()
            if self._monitor_stop.is_set():
                return
            self._probe_status(reason="requested" if requested else "periodic")
            with self._condition:
                interval = (
                    self.offline_confirmation_interval_sec
                    if self._offline_count
                    else self.status_poll_interval_ms / 1000
                )

    def _request_status_probe(self) -> None:
        self._monitor_wake.set()

    def _handle_eof(self) -> None:
        with self._condition:
            baseline_revision = self._status_revision
        self._request_status_probe()
        timeout_sec = max(
            60.0,
            (self.status_poll_interval_ms / 1000) * 2
            + self.offline_confirmation_interval_sec * self.offline_confirmations,
        )
        deadline = time.monotonic() + timeout_sec
        while True:
            request_again = False
            with self._condition:
                if self._offline_confirmed:
                    return
                if self._status_revision > baseline_revision:
                    baseline_revision = self._status_revision
                    if self._last_status_error is not None:
                        request_again = True
                    elif self._latest_snapshot.is_live:
                        snapshot = self._latest_snapshot
                        self.audit.emit(
                            "AUDIO_SOURCE_EOF_WHILE_LIVE",
                            self._last_frame_end_ms,
                            access_mode=snapshot.access_mode,
                        )
                        raise LiveAudioInterruptedError(
                            "房间仍在直播，但音频源已 EOF"
                        )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.audit.emit(
                        "AUDIO_SOURCE_EOF_STATUS_UNVERIFIED",
                        self._last_frame_end_ms,
                    )
                    raise LiveAudioInterruptedError(
                        "音频源 EOF 后无法连续确认房间已下播"
                    )
                self._condition.wait(timeout=min(1.0, remaining))
            if request_again:
                self._request_status_probe()

    def _silence_verdict(self, frame: AudioFrame) -> bool:
        """Return True when confirmed offline; raise when live audio is broken."""

        request_probe = False
        with self._condition:
            if self._offline_confirmed:
                return True
            if self._silence_probe_revision is None:
                self._silence_probe_revision = self._status_revision
                self._silence_probe_deadline = time.monotonic() + max(
                    60.0,
                    self.status_poll_interval_ms / 1000
                    + self.offline_confirmation_interval_sec
                    * self.offline_confirmations,
                )
                request_probe = True
            elif self._status_revision > self._silence_probe_revision:
                self._silence_probe_revision = self._status_revision
                if self._last_status_error is not None:
                    request_probe = True
                elif self._latest_snapshot.is_live:
                    snapshot = self._latest_snapshot
                    self.audit.emit(
                        "AUDIO_SILENCE_WHILE_LIVE",
                        frame.end_ms,
                        silence_ms=self._silent_ms,
                        access_mode=snapshot.access_mode,
                    )
                    raise LiveAudioInterruptedError(
                        f"房间仍在直播，但音频已连续静音 {self._silent_ms}ms"
                    )
            if (
                self._silence_probe_deadline is not None
                and time.monotonic() >= self._silence_probe_deadline
            ):
                self.audit.emit(
                    "AUDIO_SILENCE_STATUS_UNVERIFIED",
                    frame.end_ms,
                    silence_ms=self._silent_ms,
                )
                raise LiveAudioInterruptedError(
                    "音频持续静音且无法验证房间已下播"
                )
        if request_probe:
            self._request_status_probe()
        return False

    def read(self) -> AudioFrame | None:
        if not self._started:
            raise RuntimeError("RoomAwareAudioSource 尚未 start")
        frame = self.source.read()
        if frame is None:
            self._handle_eof()
            return None
        with self._condition:
            self._last_frame_end_ms = frame.end_ms
            if self._offline_confirmed:
                return None
        if self._rms(frame) < self.silence_rms_threshold:
            self._silent_ms += frame.duration_ms
        else:
            self._silent_ms = 0
            self._silence_probe_revision = None
            self._silence_probe_deadline = None
        if self._silent_ms >= self.silence_timeout_ms and self._silence_verdict(frame):
            return None
        return frame

    def stop(self) -> None:
        self._monitor_stop.set()
        self._monitor_wake.set()
        with self._condition:
            self._condition.notify_all()
        self.source.stop()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=1.0)
            self._monitor_thread = None
        self._started = False


class StartHookAudioSource(AudioSource):
    """先启动底层采集，再执行一次播放器等启动钩子。"""

    def __init__(self, source: AudioSource, start_hook: Callable[[], None]):
        self.source = source
        self.start_hook = start_hook
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.source.start()
        try:
            self.start_hook()
        except Exception:
            self.source.stop()
            raise
        self._started = True

    def read(self) -> AudioFrame | None:
        if not self._started:
            raise RuntimeError("StartHookAudioSource 尚未 start")
        return self.source.read()

    def stop(self) -> None:
        self.source.stop()
        self._started = False


class PreStartHookAudioSource(AudioSource):
    """Run a prerequisite immediately before starting the underlying source."""

    def __init__(self, source: AudioSource, start_hook: Callable[[], None]):
        self.source = source
        self.start_hook = start_hook
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.start_hook()
        self.source.start()
        self._started = True

    def read(self) -> AudioFrame | None:
        if not self._started:
            raise RuntimeError("PreStartHookAudioSource 尚未 start")
        return self.source.read()

    def stop(self) -> None:
        self.source.stop()
        self._started = False


class DurationLimitedAudioSource(AudioSource):
    """将任意 AudioSource 限制在指定的 Session 相对时长内。"""

    def __init__(self, source: AudioSource, max_duration_ms: int):
        if max_duration_ms <= 0:
            raise ValueError("max_duration_ms 必须为正数")
        self.source = source
        self.max_duration_ms = max_duration_ms
        self._started = False

    def start(self) -> None:
        self.source.start()
        self._started = True

    def read(self) -> AudioFrame | None:
        if not self._started:
            raise RuntimeError("DurationLimitedAudioSource 尚未 start")
        frame = self.source.read()
        if frame is None or frame.timestamp_ms >= self.max_duration_ms:
            return None
        if frame.end_ms <= self.max_duration_ms:
            return frame
        # 截断最后一帧，使归档时长不超过验证窗口。
        keep_ms = self.max_duration_ms - frame.timestamp_ms
        keep_samples = round(keep_ms * frame.sample_rate / 1000)
        byte_count = keep_samples * frame.channels * frame.sample_width
        return AudioFrame(
            timestamp_ms=frame.timestamp_ms,
            pcm=frame.pcm[:byte_count],
            sample_rate=frame.sample_rate,
            channels=frame.channels,
            sample_width=frame.sample_width,
        )

    def stop(self) -> None:
        self.source.stop()
        self._started = False


class StoppableAudioSource(AudioSource):
    """让长期 Session 能响应服务级 stop event，并正常走归档收尾。"""

    def __init__(self, source: AudioSource, stop_event: threading.Event):
        self.source = source
        self.stop_event = stop_event

    def start(self) -> None:
        self.source.start()

    def read(self) -> AudioFrame | None:
        if self.stop_event.is_set():
            return None
        frame = self.source.read()
        return None if self.stop_event.is_set() else frame

    def stop(self) -> None:
        self.source.stop()


class OffsetAudioSource(AudioSource):
    """Map a new capture process onto an existing Session wall-clock timeline."""

    def __init__(self, source: AudioSource, previous_end_ms: int, paused_at: str):
        self.source = source
        self.previous_end_ms = previous_end_ms
        self.paused_at = datetime.fromisoformat(paused_at)
        self._offset_ms: int | None = None

    def start(self) -> None:
        self.source.start()
        # A non-silence probe can buffer the first seconds of audio before
        # returning its first frame. Anchor at capture startup, not at that
        # delayed read, so the buffered audio is not counted as downtime.
        downtime_ms = max(
            0,
            round((datetime.now(timezone.utc) - self.paused_at).total_seconds() * 1000),
        )
        self._offset_ms = self.previous_end_ms + downtime_ms

    def read(self) -> AudioFrame | None:
        frame = self.source.read()
        if frame is None:
            return None
        if self._offset_ms is None:
            raise RuntimeError("OffsetAudioSource 尚未 start")
        return AudioFrame(
            timestamp_ms=frame.timestamp_ms + self._offset_ms,
            pcm=frame.pcm,
            sample_rate=frame.sample_rate,
            channels=frame.channels,
            sample_width=frame.sample_width,
        )

    def stop(self) -> None:
        self.source.stop()


class EmptyAudioSource(AudioSource):
    """Finalize a paused Session when the room went offline during downtime."""

    def read(self) -> AudioFrame | None:
        return None
