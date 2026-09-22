"""跨模块共享的数据模型。

所有时间戳均为相对于 Session 起点的毫秒数。这是音频、转写、提醒、章节和
Highlight 能够互相定位的基础。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionStatus(str, Enum):
    CREATED = "CREATED"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    AUDIO_FINALIZING = "AUDIO_FINALIZING"
    POST_PROCESSING = "POST_PROCESSING"
    DELIVERY_PENDING = "DELIVERY_PENDING"
    RETAINED_IN_STAGING = "RETAINED_IN_STAGING"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"


class InterestState(str, Enum):
    IDLE = "IDLE"
    CANDIDATE = "CANDIDATE"
    HOT = "HOT"
    COOLING = "COOLING"


@dataclass(frozen=True)
class AudioFrame:
    """一段 PCM 音频及其在 Session 时间轴上的起点。"""

    timestamp_ms: int
    pcm: bytes
    sample_rate: int = 48_000
    channels: int = 2
    sample_width: int = 2

    def __post_init__(self) -> None:
        if self.timestamp_ms < 0:
            raise ValueError("timestamp_ms 不能为负数")
        if self.sample_rate <= 0 or self.channels <= 0 or self.sample_width <= 0:
            raise ValueError("音频格式参数必须为正数")
        bytes_per_sample = self.channels * self.sample_width
        if len(self.pcm) % bytes_per_sample:
            raise ValueError("PCM 字节长度不是完整采样帧的整数倍")

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // (self.channels * self.sample_width)

    @property
    def duration_ms(self) -> int:
        return round(self.sample_count * 1000 / self.sample_rate)

    @property
    def end_ms(self) -> int:
        return self.timestamp_ms + self.duration_ms


@dataclass(frozen=True)
class SpeechSegment:
    """VAD 输出的连续语音段。"""

    start_ms: int
    end_ms: int
    pcm: bytes
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms < self.start_ms:
            raise ValueError("语音段时间范围无效")
        if self.sample_rate <= 0 or self.channels <= 0 or self.sample_width <= 0:
            raise ValueError("语音段格式参数必须为正数")
        if len(self.pcm) % (self.channels * self.sample_width):
            raise ValueError("语音段 PCM 字节长度不是完整采样帧的整数倍")

    @property
    def duration_ms(self) -> int:
        bytes_per_frame = self.channels * self.sample_width
        if not bytes_per_frame:
            return 0
        return round(len(self.pcm) / bytes_per_frame * 1000 / self.sample_rate)


@dataclass(frozen=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    confidence: float = 1.0
    final: bool = True

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms < self.start_ms:
            raise ValueError("转写时间范围无效")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence 必须在 [0, 1] 范围内")


@dataclass
class AnalysisFeatures:
    topic: str = ""
    whitelist_score: float = 0.0
    blacklist_score: float = 0.0
    information_density: float = 0.0
    structured_speech_score: float = 0.0
    topic_continuity: float = 0.0
    novelty: float = 0.0
    speech_ratio: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "whitelist_score",
            "blacklist_score",
            "information_density",
            "structured_speech_score",
            "topic_continuity",
            "novelty",
            "speech_ratio",
        ):
            value = float(getattr(self, name))
            setattr(self, name, max(0.0, min(1.0, value)))


@dataclass(frozen=True)
class SemanticResult:
    topic: str = ""
    summary: str = ""
    score: float = 0.0
    is_blacklist_topic: bool = False
    is_high_information_density: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score 必须在 [0, 1] 范围内")


@dataclass
class Highlight:
    id: str
    session_id: str
    start_ms: int
    topic: str
    score: float
    summary: str = ""
    end_ms: int | None = None
    status: str = "OPEN"

    def close(self, end_ms: int) -> None:
        if end_ms < self.start_ms:
            raise ValueError("Highlight 结束时间早于开始时间")
        self.end_ms = end_ms
        self.status = "CLOSED"


@dataclass(frozen=True)
class InterestEvent:
    event_type: str
    timestamp_ms: int
    state: InterestState
    score: float
    highlight_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    id: str
    room_id: str
    up_name: str
    start_time: str
    status: SessionStatus = SessionStatus.CREATED
    end_time: str | None = None
    duration_ms: int = 0
    final_audio_path: str | None = None
