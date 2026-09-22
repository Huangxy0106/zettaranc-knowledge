#!/usr/bin/env python3
"""Launch browser_capture_watchdog.py as a non-restarting LaunchAgent."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from live_sentinel.browser_watchdog_launchd import (
    LABEL,
    bootstrap_one_shot,
    write_one_shot_plist,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room-id", type=int, required=True)
    parser.add_argument("--bridge-pid", type=int, required=True)
    parser.add_argument("--bridge-port", type=int, required=True)
    parser.add_argument("--failsafe-label", required=True)
    parser.add_argument("--poll-sec", type=int, default=30)
    parser.add_argument("--offline-confirmations", type=int, default=3)
    parser.add_argument("--max-hours", type=float, default=13.0)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--label", default=LABEL)
    args = parser.parse_args()

    repository = Path(__file__).resolve().parents[1]
    watchdog = repository / "scripts" / "browser_capture_watchdog.py"
    logs = Path.home() / "Library" / "Logs" / "LiveSentinel"
    logs.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(watchdog),
        "--room-id",
        str(args.room_id),
        "--bridge-pid",
        str(args.bridge_pid),
        "--bridge-port",
        str(args.bridge_port),
        "--failsafe-label",
        args.failsafe_label,
        "--poll-sec",
        str(args.poll_sec),
        "--offline-confirmations",
        str(args.offline_confirmations),
        "--max-hours",
        str(args.max_hours),
        "--report",
        str(args.report.expanduser().resolve()),
    ]
    plist = write_one_shot_plist(
        program_arguments=command,
        working_directory=repository,
        stdout_path=logs / "browser-watchdog.out.log",
        stderr_path=logs / "browser-watchdog.err.log",
        label=args.label,
    )
    bootstrap_one_shot(plist, label=args.label)
    print(plist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
