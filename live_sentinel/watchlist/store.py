"""Watchlist desired/observed state 的 SQLite 存储。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import threading
import time

from ..observability import redact_payload, redact_text
from .models import WatchSource


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WatchlistStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self.initialize()

    def initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL UNIQUE,
                    requested_room_id TEXT NOT NULL,
                    creator_uid TEXT,
                    url TEXT NOT NULL,
                    name TEXT NOT NULL,
                    profile TEXT NOT NULL DEFAULT 'default',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    observed_state TEXT NOT NULL DEFAULT 'UNKNOWN',
                    last_live_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                    last_checked_at TEXT,
                    last_error TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    session_failure_count INTEGER NOT NULL DEFAULT 0,
                    browser_opened_for_live INTEGER NOT NULL DEFAULT 0,
                    capture_circuit_open INTEGER NOT NULL DEFAULT 0,
                    next_check_at REAL NOT NULL DEFAULT 0,
                    active_session_id TEXT,
                    last_session_id TEXT,
                    last_session_status TEXT,
                    session_started_for_live INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    session_id TEXT PRIMARY KEY,
                    source_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    artifact_path TEXT,
                    error TEXT,
                    FOREIGN KEY(source_id) REFERENCES sources(id)
                );
                CREATE TABLE IF NOT EXISTS service_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER,
                    event_type TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES sources(id)
                );
                """
            )
            columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(sources)")
            }
            if "session_failure_count" not in columns:
                self._connection.execute(
                    "ALTER TABLE sources ADD COLUMN session_failure_count INTEGER NOT NULL DEFAULT 0"
                )
            if "creator_uid" not in columns:
                self._connection.execute("ALTER TABLE sources ADD COLUMN creator_uid TEXT")
            if "browser_opened_for_live" not in columns:
                self._connection.execute(
                    "ALTER TABLE sources ADD COLUMN browser_opened_for_live INTEGER NOT NULL DEFAULT 0"
                )
            if "capture_circuit_open" not in columns:
                self._connection.execute(
                    "ALTER TABLE sources ADD COLUMN capture_circuit_open INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _source(row: sqlite3.Row) -> WatchSource:
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        data["session_started_for_live"] = bool(data["session_started_for_live"])
        data["browser_opened_for_live"] = bool(data["browser_opened_for_live"])
        data["capture_circuit_open"] = bool(data["capture_circuit_open"])
        return WatchSource(**data)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def append_service_event(
        self,
        source_id: int | None,
        event_type: str,
        detail: object | None = None,
        *,
        created_at: str | None = None,
    ) -> None:
        """Append one compact, redacted service-level audit event."""

        if detail is None:
            encoded = None
        elif isinstance(detail, str):
            encoded = redact_text(detail)
        else:
            encoded = json.dumps(redact_payload(detail), ensure_ascii=False, sort_keys=True)
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO service_events (source_id, event_type, detail, created_at)
                VALUES (?, ?, ?, ?)""",
                (source_id, event_type, encoded, created_at or _utc_now()),
            )

    def add_source(
        self,
        *,
        room_id: str,
        requested_room_id: str,
        creator_uid: str | None = None,
        url: str,
        name: str,
        profile: str = "default",
        enabled: bool = True,
    ) -> WatchSource:
        now = _utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO sources
                (room_id, requested_room_id, creator_uid, url, name, profile, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room_id) DO UPDATE SET
                    requested_room_id=excluded.requested_room_id,
                    creator_uid=COALESCE(excluded.creator_uid, sources.creator_uid),
                    url=excluded.url,
                    name=excluded.name,
                    profile=excluded.profile,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at""",
                (
                    room_id,
                    requested_room_id,
                    creator_uid,
                    url,
                    name,
                    profile,
                    int(enabled),
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM sources WHERE room_id = ?", (room_id,)
            ).fetchone()
        assert row is not None
        return self._source(row)

    def list_sources(self, *, enabled_only: bool = False) -> list[WatchSource]:
        sql = "SELECT * FROM sources"
        args: tuple[object, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self._connection.execute(sql, args).fetchall()
        return [self._source(row) for row in rows]

    def get_source(self, selector: str | int) -> WatchSource | None:
        with self._lock:
            if isinstance(selector, int) or str(selector).isdigit():
                value = str(selector)
                row = self._connection.execute(
                    "SELECT * FROM sources WHERE id = ? OR room_id = ?", (value, value)
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT * FROM sources WHERE name = ?", (str(selector),)
                ).fetchone()
        return self._source(row) if row is not None else None

    def set_enabled(self, selector: str | int, enabled: bool) -> WatchSource:
        source = self.get_source(selector)
        if source is None:
            raise KeyError(f"找不到关注源: {selector}")
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE sources SET enabled = ?,
                next_check_at = CASE WHEN ? = 1 THEN 0 ELSE next_check_at END,
                updated_at = ? WHERE id = ?""",
                (int(enabled), int(enabled), _utc_now(), source.id),
            )
        updated = self.get_source(source.id)
        assert updated is not None
        return updated

    def record_discovery(
        self,
        source_id: int,
        *,
        live: bool,
        next_check_at: float,
        name: str | None = None,
    ) -> None:
        now = _utc_now()
        state = "LIVE" if live else "OFFLINE"
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE sources SET
                    name = COALESCE(?, name), observed_state = ?, last_live_status = ?,
                    last_checked_at = ?, last_error = NULL, failure_count = 0,
                    next_check_at = ?,
                    session_started_for_live = CASE WHEN ? = 0 THEN 0 ELSE session_started_for_live END,
                    session_failure_count = CASE WHEN ? = 0 THEN 0 ELSE session_failure_count END,
                    browser_opened_for_live = CASE WHEN ? = 0 THEN 0 ELSE browser_opened_for_live END,
                    capture_circuit_open = CASE WHEN ? = 0 THEN 0 ELSE capture_circuit_open END,
                    updated_at = ? WHERE id = ?""",
                (
                    name,
                    state,
                    state,
                    now,
                    next_check_at,
                    int(live),
                    int(live),
                    int(live),
                    int(live),
                    now,
                    source_id,
                ),
            )
            self._connection.execute(
                """INSERT INTO service_events (source_id, event_type, detail, created_at)
                VALUES (?, 'DISCOVERY_RESULT', ?, ?)""",
                (
                    source_id,
                    json.dumps(
                        {
                            "live": live,
                            "state": state,
                            "next_check_at": next_check_at,
                            "name": name,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )

    def record_discovery_error(
        self,
        source_id: int,
        error: str,
        *,
        next_check_at: float,
    ) -> None:
        now = _utc_now()
        safe_error = redact_text(error)
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE sources SET observed_state = 'ERROR', last_checked_at = ?,
                last_error = ?, failure_count = failure_count + 1, next_check_at = ?,
                updated_at = ? WHERE id = ?""",
                (now, safe_error, next_check_at, now, source_id),
            )
            self._connection.execute(
                """INSERT INTO service_events (source_id, event_type, detail, created_at)
                VALUES (?, 'DISCOVERY_ERROR', ?, ?)""",
                (
                    source_id,
                    json.dumps(
                        {"error": safe_error, "next_check_at": next_check_at},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )

    def claim_session(self, source_id: int, session_id: str) -> bool:
        now = _utc_now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """UPDATE sources SET active_session_id = ?, last_session_id = ?,
                last_session_status = 'STARTING', observed_state = 'STARTING',
                session_started_for_live = 1, updated_at = ?
                WHERE id = ? AND enabled = 1 AND active_session_id IS NULL
                AND session_started_for_live = 0""",
                (session_id, session_id, now, source_id),
            )
            if cursor.rowcount != 1:
                return False
            self._connection.execute(
                """INSERT INTO runs (session_id, source_id, status, started_at)
                VALUES (?, ?, 'STARTING', ?)""",
                (session_id, source_id, now),
            )
            self._connection.execute(
                """INSERT INTO service_events (source_id, event_type, detail, created_at)
                VALUES (?, 'SESSION_CLAIMED', ?, ?)""",
                (source_id, session_id, now),
            )
        return True

    def mark_running(self, source_id: int, session_id: str) -> None:
        now = _utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE sources SET observed_state = 'RUNNING',
                last_session_status = 'RUNNING', updated_at = ?
                WHERE id = ? AND active_session_id = ?""",
                (now, source_id, session_id),
            )
            self._connection.execute(
                "UPDATE runs SET status = 'RUNNING' WHERE session_id = ?", (session_id,)
            )

    def mark_browser_opened(self, source_id: int) -> None:
        """记住本次 LIVE 周期已经初始化过浏览器播放页。"""

        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE sources SET browser_opened_for_live = 1, updated_at = ?
                WHERE id = ?""",
                (_utc_now(), source_id),
            )

    def open_capture_circuit(
        self,
        source_id: int,
        *,
        retry_at: float,
        detail: dict[str, object],
    ) -> bool:
        """打开静音采集熔断；返回本次是否为首次打开。"""

        now = _utc_now()
        safe_detail = json.dumps(
            redact_payload(detail), ensure_ascii=False, sort_keys=True
        )
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT capture_circuit_open FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            first_open = row is not None and not bool(row["capture_circuit_open"])
            self._connection.execute(
                """UPDATE sources SET capture_circuit_open = 1,
                browser_opened_for_live = 0, next_check_at = ?, updated_at = ?
                WHERE id = ?""",
                (retry_at, now, source_id),
            )
            self._connection.execute(
                """INSERT INTO service_events (source_id, event_type, detail, created_at)
                VALUES (?, 'CAPTURE_SILENCE_CIRCUIT_OPEN', ?, ?)""",
                (source_id, safe_detail, now),
            )
        return first_open

    def finish_session(
        self,
        source_id: int,
        session_id: str,
        *,
        status: str,
        artifact_path: str | None = None,
        error: str | None = None,
        retry_at: float | None = None,
    ) -> None:
        now = _utc_now()
        safe_error = redact_text(error) if error else None
        retryable = status in {"FAILED", "INTERRUPTED"}
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE sources SET active_session_id = NULL,
                last_session_status = ?, observed_state = ?, last_error = ?,
                next_check_at = COALESCE(?, next_check_at),
                session_started_for_live = ?,
                session_failure_count = CASE WHEN ? = 1
                    THEN session_failure_count + 1 ELSE 0 END,
                capture_circuit_open = CASE WHEN ? = 1
                    THEN capture_circuit_open ELSE 0 END,
                updated_at = ?
                WHERE id = ? AND active_session_id = ?""",
                (
                    status,
                    status,
                    safe_error,
                    retry_at,
                    0 if retryable else 1,
                    int(retryable),
                    int(retryable),
                    now,
                    source_id,
                    session_id,
                ),
            )
            self._connection.execute(
                """UPDATE runs SET status = ?, ended_at = ?, artifact_path = ?, error = ?
                WHERE session_id = ?""",
                (status, now, artifact_path, safe_error, session_id),
            )
            self._connection.execute(
                """INSERT INTO service_events (source_id, event_type, detail, created_at)
                VALUES (?, 'SESSION_FINISHED', ?, ?)""",
                (source_id, f"{session_id}:{status}", now),
            )

    def recover_orphaned_sessions(self) -> int:
        """服务重启时把遗留 RUNNING 标为中断，允许仍在线的房间重新启动。"""

        now = _utc_now()
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT id, active_session_id FROM sources WHERE active_session_id IS NOT NULL"
            ).fetchall()
            for row in rows:
                session_id = row["active_session_id"]
                self._connection.execute(
                    """UPDATE runs SET status = 'INTERRUPTED', ended_at = ?,
                    error = 'service restarted before completion' WHERE session_id = ?""",
                    (now, session_id),
                )
                self._connection.execute(
                    """UPDATE sources SET active_session_id = NULL,
                    last_session_status = 'INTERRUPTED', observed_state = 'UNKNOWN',
                    session_started_for_live = 0, next_check_at = 0,
                    session_failure_count = session_failure_count + 1,
                    updated_at = ? WHERE id = ?""",
                    (now, row["id"]),
                )
                self._connection.execute(
                    """INSERT INTO service_events (source_id, event_type, detail, created_at)
                    VALUES (?, 'SESSION_RECOVERED', ?, ?)""",
                    (
                        row["id"],
                        json.dumps(
                            {
                                "session_id": session_id,
                                "status": "INTERRUPTED",
                                "reason": "service_restarted",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
        return len(rows)

    def interrupted_session_ids(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT session_id FROM runs WHERE status = 'INTERRUPTED' ORDER BY started_at"
            ).fetchall()
        return [str(row["session_id"]) for row in rows]

    def set_run_artifact_path(self, session_id: str, artifact_path: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE runs SET artifact_path = ? WHERE session_id = ?",
                (artifact_path, session_id),
            )

    def due_sources(self, now_epoch: float | None = None) -> list[WatchSource]:
        now_value = time.time() if now_epoch is None else now_epoch
        return [
            source
            for source in self.list_sources(enabled_only=True)
            if source.active_session_id is None and source.next_check_at <= now_value
        ]

    def recent_runs(self, limit: int = 20) -> list[dict[str, object]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
