"""只保留有限时间窗口的实时转写缓存。"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from ..models import TranscriptSegment


class TranscriptBuffer:
    def __init__(self, max_seconds: int = 900):
        if max_seconds <= 0:
            raise ValueError("max_seconds 必须为正数")
        self.max_ms = max_seconds * 1000
        self._segments: deque[TranscriptSegment] = deque()

    def append(self, segment: TranscriptSegment) -> None:
        # 流式 ASR 可能先发 partial、后发同时间范围的 final；以新结果覆盖旧结果。
        remaining = [
            item
            for item in self._segments
            if not (
                item.start_ms == segment.start_ms
                and item.end_ms <= segment.end_ms
            )
        ]
        remaining.append(segment)
        remaining.sort(key=lambda item: (item.start_ms, item.end_ms))
        self._segments = deque(remaining)
        cutoff = segment.end_ms - self.max_ms
        while self._segments and self._segments[0].end_ms < cutoff:
            self._segments.popleft()

    def last(self, seconds: int, now_ms: int | None = None) -> list[TranscriptSegment]:
        if seconds <= 0:
            return []
        if not self._segments:
            return []
        end_ms = now_ms if now_ms is not None else self._segments[-1].end_ms
        cutoff = max(0, end_ms - seconds * 1000)
        return [item for item in self._segments if item.end_ms > cutoff]

    def all(self) -> list[TranscriptSegment]:
        return list(self._segments)

    def extend(self, segments: Iterable[TranscriptSegment]) -> None:
        for segment in segments:
            self.append(segment)

    def __len__(self) -> int:
        return len(self._segments)
