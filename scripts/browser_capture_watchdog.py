#!/usr/bin/env python3
"""Stop browser-audio bridge jobs after a Bilibili room is confirmed offline."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
)


def room_live_status(room_id: int, timeout: int = 20) -> int:
    request = Request(
        "https://api.live.bilibili.com/room/v1/Room/get_info"
        f"?room_id={room_id}",
        headers={"User-Agent": USER_AGENT, "Referer": "https://live.bilibili.com/"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if payload.get("code") != 0 or not isinstance(data, dict):
        raise RuntimeError(f"room status failed: code={payload.get('code')}")
    return int(data.get("live_status", 0))


def terminate_verified_receiver(pid: int, port: int) -> bool:
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    command = completed.stdout.strip()
    if completed.returncode != 0 or not command:
        return False
    if "browser_audio_receiver.py" not in command or f"--port {port}" not in command:
        raise RuntimeError(f"PID {pid} no longer matches the expected receiver")
    os.kill(pid, signal.SIGTERM)
    return True


def write_report_once(path: Path, payload: dict[str, object]) -> bool:
    """Atomically publish one terminal watchdog report without overwriting it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    try:
        os.link(temporary, path)
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


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
    args = parser.parse_args()
    if args.poll_sec <= 0 or args.offline_confirmations <= 0 or args.max_hours <= 0:
        parser.error("poll-sec, offline-confirmations, and max-hours must be positive")
    # The report is a terminal receipt for one capture. A stale launcher must
    # never repeat cleanup or overwrite the first truthful result.
    if args.report.exists():
        return 0

    deadline = time.monotonic() + args.max_hours * 3600
    consecutive_offline = 0
    checks = 0
    errors: list[str] = []
    reason = "max_runtime"
    while time.monotonic() < deadline:
        try:
            status = room_live_status(args.room_id)
            checks += 1
            consecutive_offline = consecutive_offline + 1 if status != 1 else 0
            if consecutive_offline >= args.offline_confirmations:
                reason = "confirmed_offline"
                break
        except Exception as exc:  # A transient network failure must never stop capture.
            errors.append(f"{type(exc).__name__}: {exc}")
            errors = errors[-20:]
            consecutive_offline = 0
        time.sleep(args.poll_sec)

    bridge_stopped = False
    try:
        bridge_stopped = terminate_verified_receiver(args.bridge_pid, args.bridge_port)
    except Exception as exc:
        errors.append(f"bridge cleanup: {type(exc).__name__}: {exc}")
    submitted = f"gui/{os.getuid()}/{args.failsafe_label}"
    subprocess.run(
        ["launchctl", "kill", "SIGTERM", submitted],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        ["launchctl", "remove", args.failsafe_label],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "checks": checks,
        "consecutive_offline": consecutive_offline,
        "bridge_stopped": bridge_stopped,
        "failsafe_label": args.failsafe_label,
        "output_restored": False,
        "output_policy": "keep_capture_device",
        "errors": errors,
    }
    write_report_once(args.report, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
