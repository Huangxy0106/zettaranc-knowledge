"""无依赖的能量 VAD，作为真实 VAD 模型的可替换基线。"""

from __future__ import annotations

import math

from ..models import AudioFrame, SpeechSegment


class EnergyVAD:
    def __init__(
        self,
        rms_threshold: float = 500.0,
        silence_ms: int = 600,
        max_segment_ms: int | None = 8_000,
    ):
        if rms_threshold < 0 or silence_ms < 0:
            raise ValueError("VAD 参数不能为负数")
        if max_segment_ms is not None and max_segment_ms <= 0:
            raise ValueError("max_segment_ms 必须为正数或 None")
        self.rms_threshold = rms_threshold
        self.silence_ms = silence_ms
        self.max_segment_ms = max_segment_ms
        self._start_ms: int | None = None
        self._last_speech_end_ms: int | None = None
        self._chunks: list[bytes] = []
        self._sample_rate = 16_000
        self._channels = 1
        self._sample_width = 2

    @staticmethod
    def _rms(pcm: bytes) -> float:
        if len(pcm) < 2:
            return 0.0
        total = 0
        count = 0
        for index in range(0, len(pcm) - 1, 2):
            value = int.from_bytes(pcm[index : index + 2], "little", signed=True)
            total += value * value
            count += 1
        return math.sqrt(total / count) if count else 0.0

    def _emit(self) -> SpeechSegment | None:
        if self._start_ms is None or self._last_speech_end_ms is None:
            return None
        result = SpeechSegment(
            start_ms=self._start_ms,
            end_ms=self._last_speech_end_ms,
            pcm=b"".join(self._chunks),
            sample_rate=self._sample_rate,
            channels=self._channels,
            sample_width=self._sample_width,
        )
        self._start_ms = None
        self._last_speech_end_ms = None
        self._chunks = []
        return result

    def process(self, frame: AudioFrame) -> list[SpeechSegment]:
        if frame.sample_width != 2:
            raise ValueError("EnergyVAD 目前只支持 S16LE")
        self._sample_rate = frame.sample_rate
        self._channels = frame.channels
        self._sample_width = frame.sample_width
        speaking = self._rms(frame.pcm) >= self.rms_threshold
        if speaking:
            if self._start_ms is None:
                self._start_ms = frame.timestamp_ms
            self._last_speech_end_ms = frame.end_ms
            self._chunks.append(frame.pcm)
            if (
                self.max_segment_ms is not None
                and self._start_ms is not None
                and frame.end_ms - self._start_ms >= self.max_segment_ms
            ):
                result = self._emit()
                return [result] if result else []
            return []
        if (
            self._start_ms is not None
            and self._last_speech_end_ms is not None
            and frame.end_ms - self._last_speech_end_ms >= self.silence_ms
        ):
            result = self._emit()
            return [result] if result else []
        # 短静音只用于决定分段，不混入输出 PCM，避免 end_ms 与音频长度不一致。
        return []

    def flush(self) -> list[SpeechSegment]:
        result = self._emit()
        return [result] if result else []
