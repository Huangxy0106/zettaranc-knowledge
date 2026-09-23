"""Reconcile per-session artifacts after an unclean service restart."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3


_ACTIVE_STATUSES = {
    "CREATED",
    "INITIALIZING",
    "RUNNING",
    "PAUSED",
    "STOPPING",
    "AUDIO_FINALIZING",
    "POST_PROCESSING",
    "DELIVERY_PENDING",
    "RECOVERING",
}


def mark_session_interrupted(
    root: str | Path,
    session_id: str,
    *,
    ended_at: str,
    reason: str = "service_restarted",
) -> bool:
    """Make SQLite and JSON agree with the Watchlist INTERRUPTED decision."""

    session_root = Path(root).expanduser().resolve()
    if not session_root.is_dir():
        return False
    changed = False

    database = session_root / "session.sqlite"
    if database.exists():
        connection = sqlite3.connect(database)
        try:
            row = connection.execute(
                "SELECT status, duration_ms FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is not None and str(row[0]) in _ACTIVE_STATUSES:
                changed = True
                duration_ms = int(row[1] or 0)
                with connection:
                    connection.execute(
                        """UPDATE sessions SET status = 'INTERRUPTED', end_time = ?
                        WHERE id = ?""",
                        (ended_at, session_id),
                    )
                    connection.execute(
                        """INSERT INTO events
                        (session_id, timestamp_ms, event_type, payload)
                        VALUES (?, ?, 'STAGE_FAILED', ?)""",
                        (
                            session_id,
                            duration_ms,
                            json.dumps(
                                {
                                    "state": "IDLE",
                                    "score": 0.0,
                                    "highlight_id": None,
                                    "stage": "recording",
                                    "at": ended_at,
                                    "reason": reason,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        ),
                    )
        finally:
            connection.close()

    for relative in ("session.json", "final/session.json"):
        path = session_root / relative
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("id") != session_id:
            continue
        if str(payload.get("status")) in _ACTIVE_STATUSES:
            changed = True
            payload["status"] = "INTERRUPTED"
            payload["end_time"] = ended_at
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            path.chmod(0o600)
    return changed
