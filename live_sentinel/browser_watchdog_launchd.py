"""Install the browser capture watchdog as a one-shot macOS LaunchAgent."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess


LABEL = "com.zettaranc.live-sentinel.browser-watchdog"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def default_plist_path(label: str = LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def write_one_shot_plist(
    *,
    program_arguments: list[str],
    working_directory: str | Path,
    stdout_path: str | Path,
    stderr_path: str | Path,
    destination: str | Path | None = None,
    label: str = LABEL,
) -> Path:
    if not program_arguments or not all(program_arguments):
        raise ValueError("ProgramArguments 不能为空")
    payload = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(Path(working_directory).expanduser().resolve()),
        "RunAtLoad": True,
        # A watchdog owns exactly one live capture. Successful completion is
        # terminal and must not be restarted by launchd.
        "KeepAlive": False,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": DEFAULT_PATH},
        "StandardOutPath": str(Path(stdout_path).expanduser().resolve()),
        "StandardErrorPath": str(Path(stderr_path).expanduser().resolve()),
    }
    target = Path(destination).expanduser() if destination else default_plist_path(label)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(target)
    return target


def bootstrap_one_shot(path: str | Path, *, label: str = LABEL) -> None:
    target = Path(path).expanduser().resolve()
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{label}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    completed = subprocess.run(
        ["launchctl", "bootstrap", domain, str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "watchdog launchd bootstrap 失败")
