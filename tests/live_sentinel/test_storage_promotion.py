from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from live_sentinel.config import AppConfig
from live_sentinel.models import Session, SessionStatus
from live_sentinel.runner import _finish_staged_delivery, validate_archive_root
from live_sentinel.session.manager import SessionResult
from live_sentinel.storage.promotion import promote_session


class _DeliveryManager:
    def __init__(self) -> None:
        self.store = None
        self.artifacts = None
        self.offline_finalizer = None
        self.finalizer = None
        self.calls: list[tuple[str, Path]] = []
        self.stages: list[tuple[str, str]] = []

    def record_stage(
        self,
        _result: SessionResult,
        stage: str,
        state: str,
        **_detail: object,
    ) -> None:
        self.stages.append((stage, state))

    def complete_delivery(self, result: SessionResult) -> None:
        result.session.status = SessionStatus.COMPLETED
        self.calls.append(("completed", self.artifacts.root))

    def retain_in_staging(self, result: SessionResult, _error: Exception) -> None:
        result.session.status = SessionStatus.RETAINED_IN_STAGING
        self.calls.append(("retained", self.artifacts.root))


class StoragePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _fake_session(self, session_id: str = "session-test") -> Path:
        session = self.root / "staging" / session_id
        audio = session / "audio" / "final"
        final = session / "final"
        audio.mkdir(parents=True)
        final.mkdir(parents=True)
        audio_path = audio / f"{session_id}.flac"
        audio_path.write_bytes(b"fake-flac-for-promotion-test")
        payload = {
            "id": session_id,
            "final_audio_path": str(audio_path),
            "summary": {"audio": str(audio_path)},
        }
        (session / "session.json").write_text(json.dumps(payload), encoding="utf-8")
        (final / "session.json").write_text(json.dumps(payload), encoding="utf-8")

        database = session / "session.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE sessions (final_audio_path TEXT);
                CREATE TABLE audio_segments (file_path TEXT);
                CREATE TABLE audio_final (file_path TEXT);
                """
            )
            connection.execute("INSERT INTO sessions VALUES (?)", (str(audio_path),))
            connection.execute("INSERT INTO audio_segments VALUES (?)", (str(audio_path),))
            connection.execute("INSERT INTO audio_final VALUES (?)", (str(audio_path),))
            connection.commit()
        finally:
            connection.close()
        return session

    def test_verified_promotion_rebases_metadata_then_removes_staging(self) -> None:
        source = self._fake_session()
        destination = self.root / "archive" / source.name
        result = promote_session(source, destination)

        self.assertFalse(source.exists())
        self.assertTrue((destination / "audio/final/session-test.flac").exists())
        self.assertTrue((destination / "promotion.json").exists())
        self.assertEqual((destination / "promotion.json").stat().st_mode & 0o777, 0o600)
        self.assertGreaterEqual(result.file_count, 4)
        promotion = json.loads((destination / "promotion.json").read_text())
        self.assertTrue(promotion["source_copy_verified"])
        self.assertTrue(promotion["verified_with_sha256"])
        payload = json.loads((destination / "final/session.json").read_text())
        self.assertTrue(payload["final_audio_path"].startswith(str(result.archive_root)))

        connection = sqlite3.connect(destination / "session.sqlite")
        try:
            for table, column in (
                ("sessions", "final_audio_path"),
                ("audio_segments", "file_path"),
                ("audio_final", "file_path"),
            ):
                value = connection.execute(f"SELECT {column} FROM {table}").fetchone()[0]
                self.assertTrue(value.startswith(str(result.archive_root)))
        finally:
            connection.close()

    def test_existing_destination_is_never_overwritten(self) -> None:
        source = self._fake_session("collision")
        destination = self.root / "archive" / source.name
        destination.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            promote_session(source, destination)
        self.assertTrue(source.exists())

    def test_deferred_delivery_runs_only_after_verified_promotion(self) -> None:
        source = self._fake_session("delivery-order")
        archive = self.root / "archive"
        config = AppConfig.from_mapping(
            {
                "storage": {
                    "root_dir": str(archive),
                    "require_mounted_path": False,
                    "minimum_free_gib": 0,
                }
            }
        )
        session = Session("delivery-order", "room", "up", "now")
        result = SessionResult(session=session)
        manager = _DeliveryManager()

        root = _finish_staged_delivery(
            manager=manager,  # type: ignore[arg-type]
            result=result,
            staging_root=source,
            configured_archive_root=archive,
            config=config,
        )

        self.assertEqual(root, archive.resolve() / "delivery-order")
        self.assertFalse(source.exists())
        self.assertTrue((root / "promotion.json").exists())
        self.assertEqual(manager.calls, [("completed", root)])
        self.assertEqual(
            manager.stages,
            [("t5_promotion", "STARTED"), ("t5_promotion", "SUCCEEDED")],
        )
        self.assertEqual(session.status, SessionStatus.COMPLETED)

    def test_unavailable_archive_retains_staging_and_never_claims_completed(self) -> None:
        source = self._fake_session("t5-unavailable")
        blocked_archive = self.root / "archive-is-a-file"
        blocked_archive.write_text("not a directory", encoding="utf-8")
        config = AppConfig.from_mapping(
            {
                "storage": {
                    "root_dir": str(blocked_archive),
                    "require_mounted_path": False,
                    "minimum_free_gib": 0,
                }
            }
        )
        session = Session("t5-unavailable", "room", "up", "now")
        result = SessionResult(session=session)
        manager = _DeliveryManager()

        with patch("live_sentinel.runner.LOGGER.exception"):
            root = _finish_staged_delivery(
                manager=manager,  # type: ignore[arg-type]
                result=result,
                staging_root=source,
                configured_archive_root=blocked_archive,
                config=config,
            )

        self.assertEqual(root, source)
        self.assertTrue(source.exists())
        self.assertTrue((source / "promotion_error.json").exists())
        self.assertEqual(manager.calls, [("retained", source)])
        self.assertEqual(
            manager.stages,
            [("t5_promotion", "STARTED"), ("t5_promotion", "FAILED")],
        )
        self.assertEqual(session.status, SessionStatus.RETAINED_IN_STAGING)

    def test_archive_root_accepts_a_directory_below_external_mount(self) -> None:
        config = AppConfig.from_mapping(
            {
                "storage": {
                    "root_dir": str(self.root / "external/session-root"),
                    "require_mounted_path": True,
                    "minimum_free_gib": 0,
                }
            }
        )
        with patch(
            "live_sentinel.runner._mounted_volume_for",
            return_value=self.root / "external",
        ):
            self.assertEqual(
                validate_archive_root(config),
                (self.root / "external/session-root").resolve(),
            )

    def test_archive_root_rejects_system_volume(self) -> None:
        config = AppConfig.from_mapping(
            {
                "storage": {
                    "root_dir": str(self.root / "session-root"),
                    "require_mounted_path": True,
                    "minimum_free_gib": 0,
                }
            }
        )
        with patch(
            "live_sentinel.runner._mounted_volume_for",
            return_value=Path("/"),
        ):
            with self.assertRaisesRegex(RuntimeError, "独立挂载卷"):
                validate_archive_root(config)


if __name__ == "__main__":
    unittest.main()
