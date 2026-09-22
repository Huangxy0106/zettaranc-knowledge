from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WatchSource:
    id: int
    room_id: str
    requested_room_id: str
    creator_uid: str | None
    url: str
    name: str
    profile: str
    enabled: bool
    observed_state: str
    last_live_status: str
    last_checked_at: str | None
    last_error: str | None
    failure_count: int
    session_failure_count: int
    browser_opened_for_live: bool
    capture_circuit_open: bool
    next_check_at: float
    active_session_id: str | None
    last_session_id: str | None
    last_session_status: str | None
    session_started_for_live: bool
    created_at: str
    updated_at: str
