#!/usr/bin/env python3
"""Run bounded, offline fault-injection canaries for the unattended pipeline."""

from __future__ import annotations

import json
import sys
import time
import unittest


SCENARIOS = {
    "slow_deepseek_and_ntfy_offline": (
        "tests.live_sentinel.test_session.SessionTests."
        "test_slow_judge_and_failed_notification_do_not_block_capture_reads"
    ),
    "t5_unavailable": (
        "tests.live_sentinel.test_storage_promotion.StoragePromotionTests."
        "test_unavailable_archive_retains_staging_and_never_claims_completed"
    ),
    "browser_audio_silent": (
        "tests.live_sentinel.test_archive.ArchiveTests."
        "test_non_silence_probe_fails_closed_on_silent_input"
    ),
    "ffmpeg_abnormal_eof": (
        "tests.live_sentinel.test_archive.ArchiveTests."
        "test_ffmpeg_source_reports_abnormal_eof"
    ),
}


def main() -> int:
    loader = unittest.TestLoader()
    outcomes: dict[str, dict[str, object]] = {}
    all_ok = True
    for scenario, test_name in SCENARIOS.items():
        started = time.monotonic()
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(
            loader.loadTestsFromName(test_name)
        )
        passed = result.wasSuccessful()
        all_ok = all_ok and passed
        outcomes[scenario] = {
            "passed": passed,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "failures": len(result.failures),
            "errors": len(result.errors),
        }
    print(
        json.dumps(
            {"status": "PASS" if all_ok else "FAIL", "scenarios": outcomes},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
