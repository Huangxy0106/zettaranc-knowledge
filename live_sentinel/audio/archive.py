"""高优先级的 PCM 分片归档写入器。"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path
from typing import BinaryIO

from ..models import AudioFrame
from .segment import CompletedSegment, sha256_file


class ArchiveError(RuntimeError):
    pass


class SegmentArchiveWriter:
    """把连续 PCM 写成小时级 FLAC/WAV working segment。

    分片边界按输入帧切换，因此允许略微超过配置时长；业务时间轴仍以输入帧
    的 timestamp 为准。实时链路发生阻塞不会调用此类的任何实时服务。
    """

    def __init__(
        self,
        output_dir: str | Path,
        session_id: str,
        segment_minutes: int = 120,
        sample_rate: int = 48_000,
        channels: int = 2,
        sample_width: int = 2,
        codec: str = "flac",
        ffmpeg_bin: str = "ffmpeg",
    ):
        if segment_minutes <= 0:
            raise ValueError("segment_minutes 必须为正数")
        self.output_dir = Path(output_dir)
        self.session_id = session_id
        self.segment_target_ms = segment_minutes * 60_000
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.codec = codec.lower()
        self.ffmpeg_bin = ffmpeg_bin
        self.segments: list[CompletedSegment] = []
        self.gaps: list[tuple[int, int]] = []
        self._handle: BinaryIO | wave.Wave_write | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._current_start_ms: int | None = None
        self._current_end_ms: int | None = None
        self._last_end_ms: int | None = None
        self._index = 0
        self._started = False

    @property
    def _suffix(self) -> str:
        if self.codec == "flac":
            return ".flac"
        if self.codec == "wav":
            return ".wav"
        raise ValueError("V1 仅支持 archive_codec=flac 或 wav")

    def start(self) -> None:
        if self._started:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.chmod(0o700)
        self._started = True

    def restore(self, segments: list[CompletedSegment]) -> None:
        """Continue after a clean pause without ever overwriting old audio."""

        if self._started or self.segments:
            raise ArchiveError("只能在首次 start 前恢复分片")
        ordered = sorted(segments, key=lambda item: item.start_ms)
        for segment in ordered:
            if not segment.file_path.is_file() or not segment.checksum:
                raise ArchiveError(f"旧分片不可验证: {segment.file_path}")
            if sha256_file(segment.file_path) != segment.checksum:
                raise ArchiveError(f"旧分片校验失败: {segment.file_path}")
        self.segments = ordered
        if ordered:
            self._last_end_ms = ordered[-1].end_ms
            self._index = max(int(item.id.rsplit("-", 1)[-1]) for item in ordered) + 1
        # An uncleanly terminated encoder can leave an unindexed file. Never
        # overwrite it or silently omit it from the archive.
        unknown = set(self.output_dir.glob("segment_*")) - {
            item.file_path for item in ordered
        }
        if unknown:
            raise ArchiveError(f"存在未核验的旧分片: {sorted(unknown)[0]}")

    def _open_segment(self, start_ms: int) -> None:
        self._current_start_ms = start_ms
        self._current_end_ms = start_ms
        path = self.output_dir / f"segment_{self._index:03d}_{start_ms:012d}{self._suffix}"
        if path.exists():
            raise ArchiveError(f"拒绝覆盖已有分片: {path}")
        if self.codec == "flac":
            if shutil.which(self.ffmpeg_bin) is None:
                raise ArchiveError(
                    "找不到 ffmpeg，无法写 FLAC；请安装 ffmpeg 或使用 archive_codec=wav"
                )
            if self.sample_width != 2:
                raise ArchiveError("FLAC V1 写入器目前要求 16-bit PCM")
            command = [
                self.ffmpeg_bin,
                "-loglevel",
                "error",
                "-y",
                "-f",
                "s16le",
                "-ar",
                str(self.sample_rate),
                "-ac",
                str(self.channels),
                "-i",
                "pipe:0",
                "-c:a",
                "flac",
                str(path),
            ]
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._handle = self._process.stdin
        else:
            wav_handle = wave.open(str(path), "wb")
            wav_handle.setnchannels(self.channels)
            wav_handle.setsampwidth(self.sample_width)
            wav_handle.setframerate(self.sample_rate)
            self._handle = wav_handle

    def _close_segment(self) -> None:
        if self._handle is None or self._current_start_ms is None or self._current_end_ms is None:
            return
        handle = self._handle
        self._handle = None
        if isinstance(handle, wave.Wave_write):
            handle.close()
        else:
            handle.close()
        if self._process is not None:
            process = self._process
            self._process = None
            return_code = process.wait()
            error_bytes = process.stderr.read() if process.stderr else b""
            if process.stderr is not None:
                process.stderr.close()
            if return_code != 0:
                error = error_bytes.decode(errors="replace")
                raise ArchiveError(f"FLAC 分片编码失败: {error.strip()}")
        path = self.output_dir / (
            f"segment_{self._index:03d}_{self._current_start_ms:012d}{self._suffix}"
        )
        if not path.exists() or path.stat().st_size == 0:
            raise ArchiveError(f"分片文件为空: {path}")
        path.chmod(0o600)
        self.segments.append(
            CompletedSegment(
                id=f"{self.session_id}-segment-{self._index:03d}",
                file_path=path,
                start_ms=self._current_start_ms,
                end_ms=self._current_end_ms,
                checksum=sha256_file(path),
            )
        )
        self._index += 1
        self._current_start_ms = None
        self._current_end_ms = None

    def write(self, frame: AudioFrame) -> None:
        if not self._started:
            raise RuntimeError("SegmentArchiveWriter 尚未 start")
        if (
            frame.sample_rate != self.sample_rate
            or frame.channels != self.channels
            or frame.sample_width != self.sample_width
        ):
            raise ValueError("输入音频格式与归档配置不一致")
        if self._last_end_ms is not None:
            if frame.timestamp_ms < self._last_end_ms:
                raise ArchiveError("输入音频时间戳重叠或倒退")
            if frame.timestamp_ms > self._last_end_ms:
                self.gaps.append((self._last_end_ms, frame.timestamp_ms))
        if self._handle is None:
            self._open_segment(frame.timestamp_ms)
        elif (
            self._current_start_ms is not None
            and frame.end_ms - self._current_start_ms >= self.segment_target_ms
        ):
            self._close_segment()
            self._open_segment(frame.timestamp_ms)
        assert self._handle is not None
        try:
            if isinstance(self._handle, wave.Wave_write):
                self._handle.writeframes(frame.pcm)
            else:
                self._handle.write(frame.pcm)
        except (BrokenPipeError, OSError) as exc:
            raise ArchiveError("归档分片写入失败") from exc
        self._current_end_ms = frame.end_ms
        self._last_end_ms = frame.end_ms

    def close(self) -> list[CompletedSegment]:
        if not self._started:
            return []
        self._close_segment()
        self._started = False
        return list(self.segments)
