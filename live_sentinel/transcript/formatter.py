"""最终文稿的无幻觉格式化：只做有限口语清理，不增补观点。"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..models import TranscriptSegment


def _timecode(ms: int, decimal: str = ".") -> str:
    ms = max(0, ms)
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{decimal}{millis:03d}"


def format_verbatim(segments: Iterable[TranscriptSegment]) -> str:
    blocks = []
    for segment in segments:
        if not segment.text.strip():
            continue
        blocks.append(f"[{_timecode(segment.start_ms)}]\n{segment.text.strip()}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _clean_spoken_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text.strip())
    # 只去除连续、明显无意义的口头重复；不做语义改写。
    cleaned = re.sub(r"^(嗯[,，、 ]*){2,}", "嗯，", cleaned)
    cleaned = re.sub(r"(这个这个|那个那个)", lambda match: match.group(1)[:2], cleaned)
    return cleaned


def format_readable(segments: Iterable[TranscriptSegment]) -> str:
    blocks = []
    for segment in segments:
        text = _clean_spoken_text(segment.text)
        if text:
            blocks.append(f"[{_timecode(segment.start_ms)}] {text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def format_srt(segments: Iterable[TranscriptSegment]) -> str:
    blocks = []
    for index, segment in enumerate(segments, start=1):
        text = segment.text.strip()
        if not text:
            continue
        blocks.append(
            f"{index}\n{_timecode(segment.start_ms, ',')} --> {_timecode(segment.end_ms, ',')}\n{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")
