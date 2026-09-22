"""V1 离线产物写出器。

真正的高精度 ASR 通过 ``offline_asr`` 注入。没有注入时，为了让开发链路可运行，
暂时使用实时转写作为 fallback，并在 summary 中明确标记，避免把 fallback 冒充正式
离线识别结果。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

from ..audio.finalizer import FinalAudio
from ..models import Highlight, Session, TranscriptSegment
from ..transcript.formatter import format_readable, format_srt, format_verbatim


class OfflineFinalizer:
    def __init__(
        self,
        output_dir: str | Path,
        offline_asr: Callable[[Path], Sequence[TranscriptSegment]] | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.offline_asr = offline_asr
        self.last_transcripts: list[TranscriptSegment] = []
        self.last_chapters: list[dict[str, object]] = []
        self.transcript_source: str = "uninitialized"

    def start_for_session(self) -> None:
        """按需启动由当前 Session 所拥有的离线 ASR 服务。

        普通云端 callable 没有该方法，因此这里保持无操作。只有本地 FunASR
        且配置 ``start_mode='session'`` 时，适配器才会在任务开始时预热模型。
        """

        if self.offline_asr is None:
            return
        if getattr(self.offline_asr, "start_mode", "lazy") != "session":
            return
        start = getattr(self.offline_asr, "start", None)
        if callable(start):
            start()

    def close_for_session(self) -> None:
        """关闭本 Session 自己启动的离线 ASR 子进程。"""

        if self.offline_asr is None:
            return
        if getattr(self.offline_asr, "start_mode", "lazy") != "session":
            return
        stop = getattr(self.offline_asr, "stop", None)
        if callable(stop):
            stop()

    @staticmethod
    def _json_segment(segment: TranscriptSegment) -> dict[str, object]:
        return {
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "text": segment.text,
            "confidence": segment.confidence,
        }

    def _chapters(self, highlights: Sequence[Highlight]) -> list[dict[str, object]]:
        chapters = []
        for index, highlight in enumerate(highlights, start=1):
            if highlight.end_ms is None:
                continue
            chapters.append(
                {
                    "id": f"chapter_{index:03d}",
                    "start_ms": highlight.start_ms,
                    "end_ms": highlight.end_ms,
                    "title": highlight.topic or "重点内容",
                    "summary": highlight.summary,
                    "keywords": [highlight.topic] if highlight.topic else [],
                }
            )
        return chapters

    def run(
        self,
        *,
        session: Session,
        final_audio: FinalAudio,
        realtime_transcripts: Sequence[TranscriptSegment],
        highlights: Sequence[Highlight],
    ) -> Path:
        output = self.output_dir
        output.mkdir(parents=True, exist_ok=True)
        output.chmod(0o700)
        if self.offline_asr is not None:
            transcripts = list(self.offline_asr(final_audio.file_path))
            transcript_source = "offline_asr"
        else:
            transcripts = list(realtime_transcripts)
            transcript_source = "realtime_fallback"
        self.last_transcripts = transcripts
        self.transcript_source = transcript_source
        with (output / "verbatim.jsonl").open("w", encoding="utf-8") as handle:
            for segment in transcripts:
                handle.write(json.dumps(self._json_segment(segment), ensure_ascii=False) + "\n")
        (output / "verbatim.md").write_text(format_verbatim(transcripts), encoding="utf-8")
        (output / "readable.md").write_text(format_readable(transcripts), encoding="utf-8")
        (output / "subtitles.srt").write_text(format_srt(transcripts), encoding="utf-8")
        (output / "highlights.json").write_text(
            json.dumps([highlight.__dict__ for highlight in highlights], ensure_ascii=False, default=str, indent=2),
            encoding="utf-8",
        )
        chapters = self._chapters(highlights)
        self.last_chapters = chapters
        (output / "chapters.json").write_text(
            json.dumps(chapters, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary = {
            "session_id": session.id,
            "duration_ms": session.duration_ms,
            "audio": str(final_audio.file_path),
            "transcript_source": transcript_source,
            "highlight_count": len(highlights),
            "note": (
                "未注入 offline_asr，当前逐字稿为实时转写 fallback；"
                "接入高精度 ASR 后可重新生成本目录产物。"
                if transcript_source == "realtime_fallback"
                else "逐字稿由注入的 offline_asr 生成。"
            ),
        }
        (output / "summary.md").write_text(
            "# 直播摘要\n\n"
            f"- Session: `{session.id}`\n"
            f"- 时长: `{session.duration_ms} ms`\n"
            f"- 重点片段: `{len(highlights)}`\n"
            f"- 转写来源: `{transcript_source}`\n\n"
            f"> {summary['note']}\n",
            encoding="utf-8",
        )
        (output / "session.json").write_text(
            json.dumps(self._session_payload(session, summary), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for path in output.iterdir():
            if path.is_file():
                path.chmod(0o600)
        return output

    @staticmethod
    def _session_payload(session: Session, summary: dict[str, object]) -> dict[str, object]:
        return {
            "id": session.id,
            "room_id": session.room_id,
            "up_name": session.up_name,
            "start_time": session.start_time,
            "end_time": session.end_time,
            "duration_ms": session.duration_ms,
            "status": session.status.value,
            "final_audio_path": session.final_audio_path,
            "summary": summary,
        }

    def refresh_session_metadata(self, session: Session) -> None:
        """Session 完成后刷新 session.json 的最终状态。"""

        path = self.output_dir / "session.json"
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "end_time": session.end_time,
                "duration_ms": session.duration_ms,
                "status": session.status.value,
                "final_audio_path": session.final_audio_path,
            }
        )
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)
