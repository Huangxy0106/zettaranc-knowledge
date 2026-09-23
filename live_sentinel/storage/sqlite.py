"""V1 SQLite schema 和最小读写封装。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..audio.finalizer import FinalAudio
from ..audio.segment import CompletedSegment
from ..models import Highlight, InterestEvent, Session, SessionStatus, TranscriptSegment
from ..observability import redact_payload


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self.initialize()
        self._secure_files()

    def _secure_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                path.chmod(0o600)

    def initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, room_id TEXT NOT NULL, up_name TEXT NOT NULL,
                    start_time TEXT NOT NULL, end_time TEXT, duration_ms INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL, final_audio_path TEXT
                );
                CREATE TABLE IF NOT EXISTS audio_segments (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, file_path TEXT NOT NULL,
                    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, status TEXT NOT NULL,
                    checksum TEXT, FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS audio_final (
                    session_id TEXT PRIMARY KEY, file_path TEXT NOT NULL, duration_ms INTEGER NOT NULL,
                    codec TEXT NOT NULL, sample_rate INTEGER, channels INTEGER, checksum TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS realtime_transcripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT NOT NULL,
                    confidence REAL NOT NULL, UNIQUE(session_id, start_ms, end_ms),
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS final_transcripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, speaker TEXT,
                    verbatim_text TEXT NOT NULL, readable_text TEXT NOT NULL, confidence REAL NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS topic_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, topic TEXT NOT NULL,
                    confidence REAL NOT NULL, FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS highlights (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, start_ms INTEGER NOT NULL,
                    end_ms INTEGER, topic TEXT NOT NULL, summary TEXT NOT NULL, score REAL NOT NULL,
                    status TEXT NOT NULL, FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS chapters (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
            self._secure_files()

    def create_session(self, session: Session) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO sessions
                (id, room_id, up_name, start_time, end_time, duration_ms, status, final_audio_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    room_id=excluded.room_id,
                    up_name=excluded.up_name,
                    start_time=excluded.start_time,
                    end_time=excluded.end_time,
                    duration_ms=excluded.duration_ms,
                    status=excluded.status,
                    final_audio_path=excluded.final_audio_path""",
                (
                    session.id,
                    session.room_id,
                    session.up_name,
                    session.start_time,
                    session.end_time,
                    session.duration_ms,
                    session.status.value,
                    session.final_audio_path,
                ),
            )

    def update_session(self, session: Session) -> None:
        self.create_session(session)

    def load_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return Session(
            id=row["id"], room_id=row["room_id"], up_name=row["up_name"],
            start_time=row["start_time"], status=SessionStatus(row["status"]),
            end_time=row["end_time"], duration_ms=row["duration_ms"],
            final_audio_path=row["final_audio_path"],
        )

    def load_audio_segments(self, session_id: str) -> list[CompletedSegment]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM audio_segments WHERE session_id = ? ORDER BY start_ms, id",
                (session_id,),
            ).fetchall()
        return [CompletedSegment(
            id=row["id"], file_path=Path(row["file_path"]),
            start_ms=row["start_ms"], end_ms=row["end_ms"],
            status=row["status"], checksum=row["checksum"],
        ) for row in rows]

    def load_realtime_transcripts(self, session_id: str) -> list[TranscriptSegment]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT start_ms, end_ms, text, confidence FROM realtime_transcripts "
                "WHERE session_id = ? ORDER BY start_ms, end_ms", (session_id,),
            ).fetchall()
        return [TranscriptSegment(row["start_ms"], row["end_ms"], row["text"],
                                  row["confidence"]) for row in rows]

    def add_audio_segment(self, session_id: str, segment: CompletedSegment) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO audio_segments
                (id, session_id, file_path, start_ms, end_ms, status, checksum)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    segment.id,
                    session_id,
                    str(segment.file_path),
                    segment.start_ms,
                    segment.end_ms,
                    segment.status,
                    segment.checksum,
                ),
            )

    def add_audio_final(self, final_audio: FinalAudio, sample_rate: int | None = None, channels: int | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO audio_final
                (session_id, file_path, duration_ms, codec, sample_rate, channels, checksum)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    final_audio.session_id,
                    str(final_audio.file_path),
                    final_audio.duration_ms,
                    final_audio.codec,
                    sample_rate,
                    channels,
                    final_audio.checksum,
                ),
            )

    def add_realtime_transcript(self, session_id: str, segment: TranscriptSegment) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO realtime_transcripts
                (session_id, start_ms, end_ms, text, confidence) VALUES (?, ?, ?, ?, ?)""",
                (session_id, segment.start_ms, segment.end_ms, segment.text, segment.confidence),
            )

    def add_final_transcript(self, session_id: str, segment: TranscriptSegment, readable_text: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO final_transcripts
                (session_id, start_ms, end_ms, speaker, verbatim_text, readable_text, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, segment.start_ms, segment.end_ms, None, segment.text, readable_text, segment.confidence),
            )

    def replace_final_transcripts(
        self,
        session_id: str,
        segments: list[TranscriptSegment],
    ) -> None:
        """Replace one session's offline transcript in a single transaction."""

        rows = [
            (
                session_id,
                segment.start_ms,
                segment.end_ms,
                None,
                segment.text,
                segment.text,
                segment.confidence,
            )
            for segment in segments
        ]
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM final_transcripts WHERE session_id = ?", (session_id,)
            )
            self._connection.executemany(
                """INSERT INTO final_transcripts
                (session_id, start_ms, end_ms, speaker, verbatim_text, readable_text, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def replace_chapters(
        self,
        session_id: str,
        chapters: Sequence[Mapping[str, object]],
    ) -> None:
        """Replace generated chapters so JSON and SQLite remain reconcilable."""

        rows = [
            (
                str(chapter["id"]),
                session_id,
                int(chapter["start_ms"]),
                int(chapter["end_ms"]),
                str(chapter["title"]),
                str(chapter.get("summary", "")),
            )
            for chapter in chapters
        ]
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM chapters WHERE session_id = ?", (session_id,)
            )
            self._connection.executemany(
                """INSERT INTO chapters
                (id, session_id, start_ms, end_ms, title, summary)
                VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def upsert_highlight(self, highlight: Highlight) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO highlights
                (id, session_id, start_ms, end_ms, topic, summary, score, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    highlight.id,
                    highlight.session_id,
                    highlight.start_ms,
                    highlight.end_ms,
                    highlight.topic,
                    highlight.summary,
                    highlight.score,
                    highlight.status,
                ),
            )

    def add_event(self, session_id: str, event: InterestEvent) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO events (session_id, timestamp_ms, event_type, payload) VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    event.timestamp_ms,
                    event.event_type,
                    json.dumps(
                        {
                            "state": event.state.value,
                            "score": event.score,
                            "highlight_id": event.highlight_id,
                            **redact_payload(event.payload),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )

    def count(self, table: str, session_id: str) -> int:
        if table not in {
            "audio_segments",
            "realtime_transcripts",
            "final_transcripts",
            "highlights",
            "events",
        }:
            raise ValueError("不允许查询该表")
        with self._lock:
            row = self._connection.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row["n"])
