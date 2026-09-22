"""持久化 Watchlist 的发现、去重、重试与 Session 监督。"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
import logging
from pathlib import Path
import signal
import threading
import time
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..bilibili import BilibiliRoomInfo, fetch_room_info
from ..config import AppConfig
from ..runner import BilibiliRunResult, run_bilibili_session
from ..session.recovery import mark_session_interrupted
from .models import WatchSource
from .store import WatchlistStore


LOGGER = logging.getLogger("live_sentinel.watchlist")
Discovery = Callable[[WatchSource], BilibiliRoomInfo]
Runner = Callable[[WatchSource, BilibiliRoomInfo, str], BilibiliRunResult]
Alert = Callable[[str], None]


@dataclass
class _ActiveRun:
    source_id: int
    session_id: str
    future: Future[BilibiliRunResult]


class WatchlistSupervisor:
    def __init__(
        self,
        config: AppConfig,
        store: WatchlistStore,
        *,
        discovery: Discovery | None = None,
        runner: Runner | None = None,
        alert: Alert | None = None,
        clock: Callable[[], float] = time.time,
    ):
        settings = config.watchlist
        if settings.max_concurrent_sessions <= 0:
            raise ValueError("watchlist.max_concurrent_sessions 必须为正数")
        if (
            settings.offline_poll_interval_sec <= 0
            or settings.evening_poll_interval_sec <= 0
            or settings.focus_poll_interval_sec <= 0
            or settings.live_poll_interval_sec <= 0
        ):
            raise ValueError("Watchlist 轮询间隔必须为正数")
        if any(day < 1 or day > 7 for day in settings.focus_weekdays):
            raise ValueError("watchlist.focus_weekdays 必须使用 ISO weekday 1–7")
        if settings.capture_silence_failure_limit <= 0:
            raise ValueError("watchlist.capture_silence_failure_limit 必须为正数")
        if settings.capture_silence_circuit_break_sec <= 0:
            raise ValueError("watchlist.capture_silence_circuit_break_sec 必须为正数")
        self._evening_start_minutes = self._parse_schedule_time(
            settings.evening_start_time, allow_24=False
        )
        self._evening_end_minutes = self._parse_schedule_time(
            settings.evening_end_time, allow_24=True
        )
        self._focus_start_minutes = self._parse_schedule_time(
            settings.focus_start_time, allow_24=False
        )
        self._focus_end_minutes = self._parse_schedule_time(
            settings.focus_end_time, allow_24=True
        )
        if self._evening_end_minutes <= self._evening_start_minutes:
            raise ValueError("晚间时段结束时间必须晚于开始时间")
        if self._focus_end_minutes <= self._focus_start_minutes:
            raise ValueError("重点时段结束时间必须晚于开始时间")
        try:
            self._schedule_timezone = ZoneInfo(settings.schedule_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"无效的 watchlist.schedule_timezone: {settings.schedule_timezone}"
            ) from exc
        self.config = config
        self.store = store
        self.clock = clock
        self.stop_event = threading.Event()
        self.discovery = discovery or self._discover
        self.runner = runner or self._run_source
        self.alert = alert
        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_sessions,
            thread_name_prefix="live-sentinel-session",
        )
        self._active: dict[int, _ActiveRun] = {}

    @staticmethod
    def _parse_schedule_time(value: str, *, allow_24: bool) -> int:
        try:
            hour_text, minute_text = value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"无效的重点时段时间: {value!r}") from exc
        if hour == 24 and minute == 0 and allow_24:
            return 24 * 60
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError(f"无效的重点时段时间: {value!r}")
        return hour * 60 + minute

    def _polling_window(self, now: float) -> tuple[str, float | None]:
        """返回当前轮询档位，以及下一次任一档位边界的 epoch。"""

        local_now = datetime.fromtimestamp(now, self._schedule_timezone)
        weekdays = set(self.config.watchlist.focus_weekdays)
        current_minutes = local_now.hour * 60 + local_now.minute
        in_evening = (
            self._evening_start_minutes
            <= current_minutes
            < self._evening_end_minutes
        )
        in_focus = (
            local_now.isoweekday() in weekdays
            and self._focus_start_minutes <= current_minutes < self._focus_end_minutes
        )

        boundaries: list[datetime] = []
        for day_offset in range(8):
            day = (local_now + timedelta(days=day_offset)).date()
            midnight = datetime.combine(day, datetime_time.min, self._schedule_timezone)
            evening_start = midnight + timedelta(minutes=self._evening_start_minutes)
            evening_end = midnight + timedelta(minutes=self._evening_end_minutes)
            for boundary in (evening_start, evening_end):
                if boundary > local_now:
                    boundaries.append(boundary)
            if day.isoweekday() in weekdays:
                focus_start = midnight + timedelta(minutes=self._focus_start_minutes)
                focus_end = midnight + timedelta(minutes=self._focus_end_minutes)
                for boundary in (focus_start, focus_end):
                    if boundary > local_now:
                        boundaries.append(boundary)
        next_boundary = min(boundaries).timestamp() if boundaries else None
        if in_focus:
            return "focus", next_boundary
        if in_evening:
            return "evening", next_boundary
        return "off_peak", next_boundary

    def _offline_next_check_at(self, now: float) -> float:
        window, boundary = self._polling_window(now)
        interval = {
            "focus": self.config.watchlist.focus_poll_interval_sec,
            "evening": self.config.watchlist.evening_poll_interval_sec,
            "off_peak": self.config.watchlist.offline_poll_interval_sec,
        }[window]
        next_check = now + interval
        # Never sleep across a schedule transition: at 19:00 switch to the
        # evening/focus tier, and at 24:00 return to the off-peak interval.
        if boundary is not None:
            next_check = min(next_check, boundary)
        return next_check

    def _discover(self, source: WatchSource) -> BilibiliRoomInfo:
        info = fetch_room_info(
            source.room_id,
            include_stream=True,
            timeout=self.config.watchlist.discovery_timeout_sec,
        )
        if source.creator_uid and info.creator_uid and source.creator_uid != info.creator_uid:
            raise RuntimeError(
                f"身份校验失败: configured uid={source.creator_uid}, observed uid={info.creator_uid}"
            )
        return info

    def _run_source(
        self,
        source: WatchSource,
        info: BilibiliRoomInfo,
        session_id: str,
    ) -> BilibiliRunResult:
        return run_bilibili_session(
            info,
            self.config,
            max_duration_sec=self.config.watchlist.max_session_minutes * 60,
            session_id=session_id,
            stop_event=self.stop_event,
            activate_browser=not source.browser_opened_for_live,
            browser_started_callback=lambda: self.store.mark_browser_opened(source.id),
        )

    @staticmethod
    def _is_capture_silence_failure(exc: BaseException) -> bool:
        current: BaseException | None = exc
        while current is not None:
            message = str(current)
            if "音频非静音探测失败" in message or "持续静音" in message:
                return True
            current = current.__cause__ or current.__context__
        return False

    def _send_alert(self, message: str, source_id: int) -> None:
        if self.alert is None:
            return
        try:
            self.alert(message)
        except Exception as exc:
            self.store.append_service_event(
                source_id,
                "CAPTURE_ALERT_ERROR",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            LOGGER.exception("Capture circuit alert failed")

    @staticmethod
    def _session_id(room_id: str) -> str:
        return datetime.now(timezone.utc).strftime(
            f"%Y%m%d_%H%M%S_%f_room{room_id}"
        )

    def _backoff(self, failure_count: int) -> int:
        settings = self.config.watchlist
        return min(
            settings.error_backoff_max_sec,
            settings.error_backoff_base_sec * (2 ** max(0, failure_count)),
        )

    def recover_interrupted_sessions(self) -> tuple[int, int]:
        """Recover Watchlist ownership and reconcile each Session's own artifacts."""

        recovered = self.store.recover_orphaned_sessions()
        ended_at = datetime.now(timezone.utc).isoformat()
        roots: list[Path] = []
        for configured in (
            self.config.storage.staging_dir,
            self.config.storage.root_dir,
        ):
            if not configured:
                continue
            root = Path(configured).expanduser().resolve()
            if root not in roots:
                roots.append(root)
        reconciled = 0
        for session_id in self.store.interrupted_session_ids():
            for base in roots:
                session_root = base / session_id
                if not session_root.is_dir():
                    continue
                changed = mark_session_interrupted(
                    session_root,
                    session_id,
                    ended_at=ended_at,
                )
                self.store.set_run_artifact_path(session_id, str(session_root))
                if changed:
                    reconciled += 1
                break
        return recovered, reconciled

    def _reap(self) -> None:
        now = self.clock()
        for source_id, active in list(self._active.items()):
            if not active.future.done():
                continue
            del self._active[source_id]
            try:
                run = active.future.result()
                status = run.result.session.status.value
                self.store.finish_session(
                    source_id,
                    active.session_id,
                    status=status,
                    artifact_path=str(run.root),
                )
                LOGGER.info("Session %s finished: %s", active.session_id, status)
            except Exception as exc:
                source = self.store.get_source(source_id)
                failure_count = source.session_failure_count if source is not None else 0
                retry_at = now + self._backoff(failure_count)
                silence_failure = self._is_capture_silence_failure(exc)
                new_failure_count = failure_count + 1
                circuit_due = (
                    silence_failure
                    and new_failure_count
                    >= self.config.watchlist.capture_silence_failure_limit
                )
                if circuit_due:
                    retry_at = max(
                        retry_at,
                        now + self.config.watchlist.capture_silence_circuit_break_sec,
                    )
                self.store.finish_session(
                    source_id,
                    active.session_id,
                    status="FAILED",
                    error=f"{type(exc).__name__}: {exc}",
                    retry_at=retry_at,
                )
                if circuit_due:
                    first_open = self.store.open_capture_circuit(
                        source_id,
                        retry_at=retry_at,
                        detail={
                            "session_id": active.session_id,
                            "failure_count": new_failure_count,
                            "retry_at": retry_at,
                            "reason": "continuous_capture_silence",
                        },
                    )
                    if first_open:
                        source_name = source.name if source is not None else str(source_id)
                        self._send_alert(
                            "Live Sentinel 已暂停重复录制："
                            f"{source_name} 连续 {new_failure_count} 次未检测到有效声音；"
                            f"将在 {self.config.watchlist.capture_silence_circuit_break_sec // 60} "
                            "分钟后重试一次。原始页面不会被重复打开。",
                            source_id,
                        )
                LOGGER.exception("Session %s failed", active.session_id)

    def run_once(self) -> None:
        self._reap()
        now = self.clock()
        capacity = self.config.watchlist.max_concurrent_sessions - len(self._active)
        for source in self.store.due_sources(now):
            try:
                info = self.discovery(source)
            except Exception as exc:
                delay = self._backoff(source.failure_count)
                self.store.record_discovery_error(
                    source.id,
                    f"{type(exc).__name__}: {exc}",
                    next_check_at=now + delay,
                )
                LOGGER.warning("Discovery failed for room %s: %s", source.room_id, exc)
                continue

            next_check_at = (
                now + self.config.watchlist.live_poll_interval_sec
                if info.is_live
                else self._offline_next_check_at(now)
            )
            self.store.record_discovery(
                source.id,
                live=info.is_live,
                next_check_at=next_check_at,
                name=info.uname if source.name == source.room_id else None,
            )
            refreshed = self.store.get_source(source.id)
            if not info.is_live or refreshed is None:
                continue
            if refreshed.session_started_for_live or capacity <= 0:
                continue
            session_id = self._session_id(source.room_id)
            if not self.store.claim_session(source.id, session_id):
                continue
            try:
                future = self._executor.submit(self.runner, refreshed, info, session_id)
            except Exception as exc:
                self.store.finish_session(
                    source.id,
                    session_id,
                    status="FAILED",
                    error=f"{type(exc).__name__}: {exc}",
                    retry_at=now + self._backoff(refreshed.session_failure_count),
                )
                raise
            self.store.mark_running(source.id, session_id)
            self._active[source.id] = _ActiveRun(source.id, session_id, future)
            capacity -= 1
            LOGGER.info("Started Session %s for room %s", session_id, source.room_id)

    def run_forever(self) -> None:
        recovered, reconciled = self.recover_interrupted_sessions()
        if recovered:
            LOGGER.warning("Recovered %d interrupted Session(s)", recovered)
        if reconciled:
            LOGGER.info("Reconciled %d interrupted Session artifact set(s)", reconciled)

        def request_stop(_signum: int, _frame: object) -> None:
            self.stop_event.set()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
        LOGGER.info("Watchlist service started; state=%s", self.store.path)
        try:
            while not self.stop_event.is_set():
                self.run_once()
                self.stop_event.wait(1.0)
        finally:
            # Active audio sources own their ffmpeg lifecycle. Do not accept new
            # work; already-running sessions get a bounded opportunity to finish.
            self._executor.shutdown(wait=True, cancel_futures=True)
            LOGGER.info("Watchlist service stopped")

    @property
    def active_count(self) -> int:
        return len(self._active)
