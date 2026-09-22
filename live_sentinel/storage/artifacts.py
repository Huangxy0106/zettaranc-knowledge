"""Session 目录下的实时 JSONL 审计文件。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import InterestEvent, Session, TranscriptSegment
from ..observability import redact_payload


class SessionArtifacts:
    """把实时过程以 append-only JSONL 形式落盘，便于故障后检查和重放。"""

    def __init__(self, session_root: str | Path):
        self.root = Path(session_root)
        self.realtime_dir = self.root / "realtime"
        self.realtime_dir.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.realtime_dir.chmod(0o700)

    def _append(self, name: str, payload: dict[str, Any]) -> None:
        with (self.realtime_dir / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        (self.realtime_dir / name).chmod(0o600)

    def append_transcript(self, segment: TranscriptSegment) -> None:
        self._append(
            "transcript.jsonl",
            {
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "text": segment.text,
                "confidence": segment.confidence,
            },
        )

    def append_analysis(self, event: InterestEvent) -> None:
        self._append(
            "analysis.jsonl",
            {
                "timestamp_ms": event.timestamp_ms,
                "state": event.state.value,
                "score": event.score,
                **redact_payload(event.payload),
            },
        )

    def append_event(self, event: InterestEvent) -> None:
        self._append(
            "events.jsonl",
            {
                "timestamp_ms": event.timestamp_ms,
                "event_type": event.event_type,
                "state": event.state.value,
                "score": event.score,
                "highlight_id": event.highlight_id,
                **redact_payload(event.payload),
            },
        )

    def write_session(self, session: Session) -> None:
        payload = {
            "id": session.id,
            "room_id": session.room_id,
            "up_name": session.up_name,
            "start_time": session.start_time,
            "end_time": session.end_time,
            "duration_ms": session.duration_ms,
            "status": session.status.value,
            "final_audio_path": session.final_audio_path,
        }
        path = self.root / "session.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        path.chmod(0o600)
