from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from live_sentinel.bilibili import BilibiliRoomInfo, room_id_from_reference
from live_sentinel.config import AppConfig
from live_sentinel.models import Session, SessionStatus
from live_sentinel.runner import BilibiliRunResult
from live_sentinel.session.manager import SessionResult
from live_sentinel.storage.artifacts import SessionArtifacts
from live_sentinel.storage.sqlite import SQLiteStore
from live_sentinel.watchlist.store import WatchlistStore
from live_sentinel.watchlist.supervisor import WatchlistSupervisor


class WatchlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WatchlistStore(self.root / "watchlist.sqlite3")
        self.source = self.store.add_source(
            room_id="5436512",
            requested_room_id="5436512",
            creator_uid="326246517",
            url="https://live.bilibili.com/5436512",
            name="测试主播",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _config(self) -> AppConfig:
        return AppConfig.from_mapping(
            {
                "watchlist": {
                    "state_db": str(self.root / "watchlist.sqlite3"),
                    "offline_poll_interval_sec": 10,
                    "focus_poll_interval_sec": 5,
                    "live_poll_interval_sec": 2,
                    "error_backoff_base_sec": 3,
                    "error_backoff_max_sec": 30,
                    "max_concurrent_sessions": 1,
                    "max_session_minutes": 1,
                }
            }
        )

    @staticmethod
    def _info(live: bool) -> BilibiliRoomInfo:
        return BilibiliRoomInfo(
            room_id=5436512,
            requested_room_id=5436512,
            creator_uid="326246517",
            uname="测试主播",
            title="测试直播",
            live_status=1 if live else 0,
            stream_url="https://example.test/live.flv" if live else None,
        )

    def test_room_reference_accepts_id_and_url(self) -> None:
        self.assertEqual(room_id_from_reference("5436512"), 5436512)
        self.assertEqual(
            room_id_from_reference("https://live.bilibili.com/5436512?foo=1"), 5436512
        )
        with self.assertRaises(ValueError):
            room_id_from_reference("https://example.test/not-a-room")

    def test_source_persists_and_enable_disable_changes_desired_state(self) -> None:
        self.store.record_discovery(
            self.source.id, live=False, next_check_at=999.0
        )
        self.store.set_enabled(self.source.id, False)
        self.store.close()
        self.store = WatchlistStore(self.root / "watchlist.sqlite3")
        restored = self.store.get_source(self.source.id)
        assert restored is not None
        self.assertFalse(restored.enabled)
        self.assertEqual(restored.room_id, "5436512")
        self.assertEqual(restored.creator_uid, "326246517")
        self.store.set_enabled(self.source.id, True)
        enabled = self.store.get_source(self.source.id)
        assert enabled is not None
        self.assertTrue(enabled.enabled)
        self.assertEqual(enabled.next_check_at, 0)

    def test_each_discovery_appends_a_history_event(self) -> None:
        self.store.record_discovery(self.source.id, live=False, next_check_at=100.0)
        self.store.record_discovery(self.source.id, live=True, next_check_at=200.0)
        rows = self.store._connection.execute(
            "SELECT detail FROM service_events WHERE event_type = 'DISCOVERY_RESULT' ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn('"state": "OFFLINE"', rows[0][0])
        self.assertIn('"state": "LIVE"', rows[1][0])

    def test_one_session_per_live_epoch_then_rearms_after_offline(self) -> None:
        now = [100.0]
        discoveries = [self._info(True), self._info(True), self._info(False), self._info(True)]
        started: list[str] = []

        def discover(_source):
            return discoveries.pop(0)

        def run(source, _info, session_id):
            started.append(session_id)
            session = Session(
                id=session_id,
                room_id=source.room_id,
                up_name=source.name,
                start_time=datetime.now(timezone.utc).isoformat(),
                status=SessionStatus.COMPLETED,
            )
            return BilibiliRunResult(self.root / session_id, SessionResult(session))

        supervisor = WatchlistSupervisor(
            self._config(), self.store, discovery=discover, runner=run, clock=lambda: now[0]
        )
        supervisor.run_once()  # starts first Session
        supervisor.run_once()  # reaps; still in the same LIVE epoch
        now[0] += 3
        supervisor.run_once()  # sees LIVE again, but does not duplicate
        self.assertEqual(len(started), 1)
        now[0] += 3
        supervisor.run_once()  # sees OFFLINE and rearms
        now[0] += 11
        supervisor.run_once()  # next LIVE starts a new Session
        self.assertEqual(len(started), 2)
        supervisor.stop_event.set()
        supervisor._executor.shutdown(wait=True)

    def test_discovery_error_records_exponential_backoff(self) -> None:
        def fail(_source):
            raise TimeoutError("network")

        supervisor = WatchlistSupervisor(
            self._config(), self.store, discovery=fail, clock=lambda: 100.0
        )
        with self.assertLogs("live_sentinel.watchlist", level="WARNING"):
            supervisor.run_once()
        source = self.store.get_source(self.source.id)
        assert source is not None
        self.assertEqual(source.observed_state, "ERROR")
        self.assertEqual(source.failure_count, 1)
        self.assertEqual(source.next_check_at, 103.0)
        self.assertIn("TimeoutError", source.last_error or "")
        supervisor.stop_event.set()
        supervisor._executor.shutdown(wait=True)

    def test_offline_polling_switches_at_focus_boundaries(self) -> None:
        config = self._config()
        config.watchlist.offline_poll_interval_sec = 10_800
        config.watchlist.evening_poll_interval_sec = 3_600
        config.watchlist.evening_start_time = "19:00"
        config.watchlist.evening_end_time = "24:00"
        config.watchlist.focus_poll_interval_sec = 300
        config.watchlist.focus_weekdays = [3, 7]
        config.watchlist.focus_start_time = "19:00"
        config.watchlist.focus_end_time = "24:00"
        config.watchlist.schedule_timezone = "Asia/Shanghai"
        supervisor = WatchlistSupervisor(config, self.store)
        china = ZoneInfo("Asia/Shanghai")

        def epoch(year, month, day, hour, minute):
            return datetime(year, month, day, hour, minute, tzinfo=china).timestamp()

        mon_1630 = epoch(2026, 9, 14, 16, 30)
        self.assertEqual(supervisor._offline_next_check_at(mon_1630), mon_1630 + 9_000)

        mon_1900 = epoch(2026, 9, 14, 19, 0)
        self.assertEqual(supervisor._offline_next_check_at(mon_1900), mon_1900 + 3_600)

        mon_2330 = epoch(2026, 9, 14, 23, 30)
        self.assertEqual(supervisor._offline_next_check_at(mon_2330), mon_2330 + 1_800)

        wed_1858 = epoch(2026, 9, 9, 18, 58)
        self.assertEqual(supervisor._offline_next_check_at(wed_1858), wed_1858 + 120)

        wed_1900 = epoch(2026, 9, 9, 19, 0)
        self.assertEqual(supervisor._offline_next_check_at(wed_1900), wed_1900 + 300)

        wed_2358 = epoch(2026, 9, 9, 23, 58)
        self.assertEqual(supervisor._offline_next_check_at(wed_2358), wed_2358 + 120)

        sun_2000 = epoch(2026, 9, 13, 20, 0)
        self.assertEqual(supervisor._offline_next_check_at(sun_2000), sun_2000 + 300)

        thu_midnight = epoch(2026, 9, 10, 0, 0)
        self.assertEqual(
            supervisor._offline_next_check_at(thu_midnight), thu_midnight + 10_800
        )
        supervisor.stop_event.set()
        supervisor._executor.shutdown(wait=True)

    def test_invalid_focus_schedule_is_rejected(self) -> None:
        config = self._config()
        config.watchlist.focus_weekdays = [0]
        with self.assertRaisesRegex(ValueError, "ISO weekday"):
            WatchlistSupervisor(config, self.store)

    def test_session_failures_back_off_and_remain_retryable(self) -> None:
        now = [100.0]

        def fail_run(_source, _info, _session_id):
            raise RuntimeError("ffmpeg failed")

        supervisor = WatchlistSupervisor(
            self._config(),
            self.store,
            discovery=lambda _source: self._info(True),
            runner=fail_run,
            clock=lambda: now[0],
        )
        supervisor.run_once()
        # Waiting on the Future is deterministic; _reap then records the retry.
        try:
            supervisor._active[self.source.id].future.result(timeout=2)
        except RuntimeError:
            pass
        with self.assertLogs("live_sentinel.watchlist", level="ERROR"):
            supervisor.run_once()
        source = self.store.get_source(self.source.id)
        assert source is not None
        self.assertEqual(source.last_session_status, "FAILED")
        self.assertEqual(source.session_failure_count, 1)
        self.assertEqual(source.next_check_at, 103.0)
        self.assertFalse(source.session_started_for_live)
        supervisor.stop_event.set()
        supervisor._executor.shutdown(wait=True)

    def test_browser_activation_is_skipped_after_first_open_in_live_epoch(self) -> None:
        self.store.mark_browser_opened(self.source.id)
        source = self.store.get_source(self.source.id)
        assert source is not None
        supervisor = WatchlistSupervisor(self._config(), self.store)
        with patch(
            "live_sentinel.watchlist.supervisor.run_bilibili_session"
        ) as run_session:
            supervisor._run_source(source, self._info(True), "session-browser")
        self.assertFalse(run_session.call_args.kwargs["activate_browser"])
        supervisor.stop_event.set()
        supervisor._executor.shutdown(wait=True)

    def test_repeated_capture_silence_opens_one_circuit_and_rearms_browser(self) -> None:
        now = [100.0]
        alerts: list[str] = []
        attempts = [0]

        def fail_silently(_source, _info, _session_id):
            attempts[0] += 1
            self.store.mark_browser_opened(self.source.id)
            raise RuntimeError("音频非静音探测失败：20000ms 内仅检测到 0ms 有效声音")

        config = self._config()
        config.watchlist.capture_silence_failure_limit = 2
        config.watchlist.capture_silence_circuit_break_sec = 120
        supervisor = WatchlistSupervisor(
            config,
            self.store,
            discovery=lambda _source: self._info(True),
            runner=fail_silently,
            alert=alerts.append,
            clock=lambda: now[0],
        )

        for expected_failure in (1, 2):
            supervisor.run_once()
            try:
                supervisor._active[self.source.id].future.result(timeout=2)
            except RuntimeError:
                pass
            with self.assertLogs("live_sentinel.watchlist", level="ERROR"):
                supervisor.run_once()
            source = self.store.get_source(self.source.id)
            assert source is not None
            self.assertEqual(source.session_failure_count, expected_failure)
            if expected_failure == 1:
                now[0] = source.next_check_at

        source = self.store.get_source(self.source.id)
        assert source is not None
        self.assertTrue(source.capture_circuit_open)
        self.assertFalse(source.browser_opened_for_live)
        self.assertGreaterEqual(source.next_check_at, 220.0)
        self.assertEqual(len(alerts), 1)
        event = self.store._connection.execute(
            "SELECT event_type FROM service_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event[0], "CAPTURE_SILENCE_CIRCUIT_OPEN")

        self.store.record_discovery(self.source.id, live=False, next_check_at=999.0)
        reset = self.store.get_source(self.source.id)
        assert reset is not None
        self.assertFalse(reset.capture_circuit_open)
        self.assertFalse(reset.browser_opened_for_live)
        supervisor.stop_event.set()
        supervisor._executor.shutdown(wait=True)

    def test_orphaned_session_is_recoverable(self) -> None:
        self.assertTrue(self.store.claim_session(self.source.id, "session-a"))
        self.store.mark_running(self.source.id, "session-a")
        self.assertEqual(self.store.recover_orphaned_sessions(), 1)
        source = self.store.get_source(self.source.id)
        assert source is not None
        self.assertIsNone(source.active_session_id)
        self.assertEqual(source.last_session_status, "PAUSED")
        self.assertFalse(source.session_started_for_live)

    def test_paused_session_reuses_id_and_finishes_when_room_is_offline(self) -> None:
        seen: list[tuple[str, bool]] = []

        def discover(_source):
            return self._info(len(seen) == 0)

        def run(source, info, session_id):
            seen.append((session_id, info.is_live))
            session = Session(
                session_id, source.room_id, source.name, "original-start",
                status=SessionStatus.PAUSED if len(seen) == 1 else SessionStatus.COMPLETED,
            )
            return BilibiliRunResult(self.root / session_id, SessionResult(session))

        supervisor = WatchlistSupervisor(
            self._config(), self.store, discovery=discover, runner=run,
            clock=lambda: 100.0,
        )
        supervisor.run_once()
        next(iter(supervisor._active.values())).future.result()
        supervisor.run_once()
        next(iter(supervisor._active.values())).future.result()
        supervisor.run_once()
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertEqual(seen[1][1], False)
        self.assertEqual(self.store.recent_runs()[0]["status"], "COMPLETED")
        supervisor.stop_event.set()
        supervisor._executor.shutdown(wait=True)

    def test_recovery_reconciles_watchlist_and_session_artifacts(self) -> None:
        config = self._config()
        config.storage.staging_dir = str(self.root / "staging")
        config.storage.root_dir = str(self.root / "archive")
        session_id = "session-reconcile"
        self.assertTrue(self.store.claim_session(self.source.id, session_id))
        self.store.mark_running(self.source.id, session_id)

        session_root = Path(config.storage.staging_dir) / session_id
        store = SQLiteStore(session_root / "session.sqlite")
        session = Session(
            session_id,
            self.source.room_id,
            self.source.name,
            datetime.now(timezone.utc).isoformat(),
            status=SessionStatus.RUNNING,
        )
        store.create_session(session)
        SessionArtifacts(session_root).write_session(session)
        store.close()

        supervisor = WatchlistSupervisor(config, self.store)
        recovered, reconciled = supervisor.recover_interrupted_sessions()
        self.assertEqual((recovered, reconciled), (1, 1))
        payload = json.loads((session_root / "session.json").read_text())
        self.assertEqual(payload["status"], "INTERRUPTED")
        recovered_store = SQLiteStore(session_root / "session.sqlite")
        try:
            row = recovered_store._connection.execute(
                "SELECT status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            self.assertEqual(row[0], "INTERRUPTED")
            event = recovered_store._connection.execute(
                "SELECT event_type, payload FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(event[0], "STAGE_FAILED")
            self.assertIn('"stage": "recording"', event[1])
        finally:
            recovered_store.close()
            supervisor.stop_event.set()
            supervisor._executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
