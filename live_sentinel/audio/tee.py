"""将同一份音频安全分发给归档链路和实时链路。"""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Full, Queue

from ..models import AudioFrame


@dataclass
class TeeStats:
    published: int = 0
    realtime_dropped: int = 0


class AudioTee:
    """归档队列优先，实时队列拥塞时允许降级丢帧。"""

    def __init__(self, archive_maxsize: int = 128, realtime_maxsize: int = 32):
        if archive_maxsize <= 0 or realtime_maxsize <= 0:
            raise ValueError("队列容量必须为正数")
        self.archive_queue: Queue[AudioFrame] = Queue(maxsize=archive_maxsize)
        self.realtime_queue: Queue[AudioFrame] = Queue(maxsize=realtime_maxsize)
        self.stats = TeeStats()

    def publish(
        self,
        frame: AudioFrame,
        archive_timeout: float | None = None,
        *,
        include_realtime: bool = True,
    ) -> None:
        # P0 归档队列使用阻塞写入，不能因实时分析拥塞而丢失。
        self.archive_queue.put(frame, timeout=archive_timeout)
        if include_realtime:
            try:
                self.realtime_queue.put_nowait(frame)
            except Full:
                self.stats.realtime_dropped += 1
        self.stats.published += 1

    def get_archive(self, timeout: float | None = None) -> AudioFrame | None:
        try:
            return self.archive_queue.get(timeout=timeout)
        except Empty:
            return None

    def get_realtime(self, timeout: float | None = None) -> AudioFrame | None:
        try:
            return self.realtime_queue.get(timeout=timeout)
        except Empty:
            return None
