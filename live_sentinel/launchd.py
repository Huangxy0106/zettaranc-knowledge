"""macOS launchd 用户服务安装器。"""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import sys


LABEL = "com.zettaranc.live-sentinel.watchlist"
DEFAULT_SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def write_plist(
    *,
    config_path: str | Path | None,
    env_file: str | Path | None,
    state_db: str | Path | None,
    working_directory: str | Path,
) -> Path:
    logs = Path.home() / "Library" / "Logs" / "LiveSentinel"
    logs.mkdir(parents=True, exist_ok=True)
    for name in (
        "watchlist.err.log",
        "watchlist.launchd.err.log",
        "watchlist.out.log",
    ):
        log_path = logs / name
        log_path.touch(exist_ok=True)
        log_path.chmod(0o600)
    arguments = [sys.executable, "-m", "live_sentinel.cli"]
    if config_path is not None:
        arguments.extend(["--config", str(Path(config_path).expanduser().resolve())])
    if env_file is not None:
        arguments.extend(["--env-file", str(Path(env_file).expanduser().resolve())])
    if state_db is not None:
        arguments.extend(["--state-db", str(Path(state_db).expanduser().resolve())])
    arguments.extend(["service", "run"])
    payload = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(Path(working_directory).resolve()),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        # Continuous CoreAudio capture is latency-sensitive. Background jobs
        # receive CPU and I/O throttling that can make AVFoundation deliver
        # audio far slower than wall time, corrupting the archive timeline.
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": DEFAULT_SERVICE_PATH},
        "StandardOutPath": str(logs / "watchlist.out.log"),
        # Python logging owns watchlist.err.log and rotates it daily. Keep raw
        # launcher stderr separate so launchd never holds the rotated file open.
        "StandardErrorPath": str(logs / "watchlist.launchd.err.log"),
    }
    destination = plist_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(destination)
    return destination


def bootstrap(path: Path | None = None) -> None:
    target = path or plist_path()
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(target)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    completed = subprocess.run(
        ["launchctl", "bootstrap", domain, str(target)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "launchctl bootstrap 失败")


def service_status() -> tuple[bool, str]:
    completed = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return False, completed.stderr.strip() or "not loaded"
    state = "loaded"
    for line in completed.stdout.splitlines():
        if "state =" in line:
            state = line.strip()
            break
    return True, state


def uninstall() -> bool:
    target = plist_path()
    if not target.exists():
        return False
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(target)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    target.unlink()
    return True
