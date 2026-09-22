"""内部音频分片和 Session 时间轴校验。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class TimelineGap:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(frozen=True)
class CompletedSegment:
    id: str
    file_path: Path
    start_ms: int
    end_ms: int
    status: str = "COMPLETED"
    checksum: str | None = None

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_timeline(
    segments: Iterable[CompletedSegment],
) -> tuple[list[CompletedSegment], list[TimelineGap]]:
    """按起点排序，拒绝重叠，返回允许存在的时间空洞。"""

    ordered = sorted(segments, key=lambda item: (item.start_ms, item.end_ms))
    gaps: list[TimelineGap] = []
    previous: CompletedSegment | None = None
    for segment in ordered:
        if segment.end_ms <= segment.start_ms:
            raise ValueError(f"分片 {segment.id} 的时间范围无效")
        if not segment.file_path.exists():
            raise FileNotFoundError(segment.file_path)
        if previous is not None:
            if segment.start_ms < previous.end_ms:
                raise ValueError(
                    f"分片时间重叠: {previous.id} 与 {segment.id}"
                )
            if segment.start_ms > previous.end_ms:
                gaps.append(TimelineGap(previous.end_ms, segment.start_ms))
        previous = segment
    return ordered, gaps
