"""将 working segment 无损合并为一场直播一个 Final Audio。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable

from .segment import CompletedSegment, TimelineGap, sha256_file, validate_timeline


@dataclass(frozen=True)
class FinalAudio:
    session_id: str
    file_path: Path
    duration_ms: int
    codec: str
    checksum: str
    gaps: tuple[TimelineGap, ...] = ()


class AudioFinalizer:
    def __init__(
        self,
        output_dir: str | Path,
        codec: str = "flac",
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
    ):
        self.output_dir = Path(output_dir)
        self.codec = codec.lower()
        if self.codec not in {"flac", "wav"}:
            raise ValueError("V1 Finalizer 仅支持 codec=flac 或 wav")
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin

    def _probe_duration_ms(self, path: Path) -> int:
        if self.codec == "wav" and shutil.which(self.ffprobe_bin) is None:
            with wave.open(str(path), "rb") as handle:
                if handle.getframerate() <= 0:
                    raise RuntimeError("WAV 文件采样率无效")
                return round(handle.getnframes() * 1000 / handle.getframerate())
        if shutil.which(self.ffprobe_bin) is None:
            raise RuntimeError("找不到 ffprobe，无法执行 Final Audio 校验")
        result = subprocess.run(
            [
                self.ffprobe_bin,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return round(float(result.stdout.strip()) * 1000)

    def _decode_test(self, path: Path) -> None:
        if self.codec == "wav" and shutil.which(self.ffmpeg_bin) is None:
            with wave.open(str(path), "rb") as handle:
                while handle.readframes(16_384):
                    pass
            return
        if shutil.which(self.ffmpeg_bin) is None:
            raise RuntimeError("找不到 ffmpeg，无法执行 Decode Test")
        result = subprocess.run(
            [self.ffmpeg_bin, "-v", "error", "-i", str(path), "-f", "null", "-"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.stderr.strip():
            raise RuntimeError(
                "Final Audio 解码测试报告错误: " + result.stderr.strip()[-1_000:]
            )

    def _concat(self, ordered: list[CompletedSegment], output: Path) -> None:
        if len(ordered) == 1:
            shutil.copy2(ordered[0].file_path, output)
            return
        if self.codec == "wav" and shutil.which(self.ffmpeg_bin) is None:
            with wave.open(str(ordered[0].file_path), "rb") as first:
                params = first.getparams()
                with wave.open(str(output), "wb") as writer:
                    writer.setparams(params)
                    writer.writeframes(first.readframes(first.getnframes()))
                    for segment in ordered[1:]:
                        with wave.open(str(segment.file_path), "rb") as source:
                            if source.getparams()[:4] != params[:4]:
                                raise ValueError("WAV 分片格式不一致，无法合并")
                            writer.writeframes(source.readframes(source.getnframes()))
            return
        if shutil.which(self.ffmpeg_bin) is None:
            raise RuntimeError("多个音频分片合并需要 ffmpeg")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", delete=False
        ) as handle:
            list_path = Path(handle.name)
            for segment in ordered:
                safe_path = str(segment.file_path.resolve()).replace("'", "'\\''")
                handle.write(f"file '{safe_path}'\n")
        codec = "flac" if self.codec == "flac" else "pcm_s16le"
        try:
            # Do not stream-copy independently encoded FLAC segments. That can
            # produce a chained file whose PCM is complete but whose container
            # duration and timestamps describe only the first segment. Decode
            # and losslessly encode one continuous stream, rebuilding timestamps
            # from the audio sample count.
            subprocess.run(
                [
                    self.ffmpeg_bin,
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_path),
                    "-map",
                    "0:a:0",
                    "-af",
                    "asetpts=N/SR/TB",
                    "-c:a",
                    codec,
                    str(output),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        finally:
            list_path.unlink(missing_ok=True)

    def finalize(
        self,
        session_id: str,
        segments: list[CompletedSegment],
        session_duration_ms: int | None = None,
        gaps: Iterable[TimelineGap] | None = None,
        tolerance_ms: int = 1_500,
    ) -> FinalAudio:
        ordered, timeline_gaps = validate_timeline(segments)
        expected_suffix = ".flac" if self.codec == "flac" else ".wav"
        mismatched = [segment.file_path for segment in ordered if segment.file_path.suffix.lower() != expected_suffix]
        if mismatched:
            raise ValueError(
                f"Finalizer codec={self.codec} 与分片后缀不一致: {mismatched[0]}"
            )
        all_gaps = list(timeline_gaps)
        if gaps is not None:
            all_gaps.extend(gaps)
        unique_gaps = {
            (gap.start_ms, gap.end_ms): gap for gap in all_gaps
        }
        all_gaps = sorted(unique_gaps.values(), key=lambda gap: (gap.start_ms, gap.end_ms))
        if not ordered:
            raise ValueError("没有可合并的已完成音频分片")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.chmod(0o700)
        suffix = ".flac" if self.codec == "flac" else ".wav"
        output = self.output_dir / f"{session_id}{suffix}"
        with tempfile.NamedTemporaryFile(
            dir=self.output_dir,
            prefix=f".{session_id}.",
            suffix=f".partial{suffix}",
            delete=False,
        ) as handle:
            partial = Path(handle.name)
        # 分片之间的时间空洞已经不包含在分片 duration 中，不能再次扣除；
        # 只有发生在单个分片内部、且由采集器显式报告的空洞才需要扣除。
        internal_gap_ms = sum(
            gap.duration_ms
            for gap in all_gaps
            if any(
                segment.start_ms <= gap.start_ms
                and gap.end_ms <= segment.end_ms
                for segment in ordered
            )
        )
        expected_ms = sum(segment.duration_ms for segment in ordered) - internal_gap_ms
        if session_duration_ms is not None:
            expected_ms = max(0, session_duration_ms - sum(g.duration_ms for g in all_gaps))
        try:
            self._concat(ordered, partial)
            self._decode_test(partial)
            duration_ms = self._probe_duration_ms(partial)
            if abs(duration_ms - expected_ms) > tolerance_ms * max(1, len(ordered)):
                raise ValueError(
                    f"Final Audio 时长校验失败: actual={duration_ms}ms expected≈{expected_ms}ms"
                )
            checksum = sha256_file(partial)
            partial.chmod(0o600)
            partial.replace(output)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        return FinalAudio(
            session_id=session_id,
            file_path=output,
            duration_ms=duration_ms,
            codec=self.codec,
            checksum=checksum,
            gaps=tuple(all_gaps),
        )
