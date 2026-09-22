#!/usr/bin/env python3
"""把一个已经完成的 Session 目录备份到百度网盘（bypy）。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_sentinel.backup.baidu import BaiduNetdiskBackup


def main() -> int:
    parser = argparse.ArgumentParser(description="上传完整 Session 目录到百度网盘")
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--session-id", help="远端目录名；默认使用本地目录名")
    parser.add_argument("--command", default="bypy", help="bypy 可执行文件名")
    parser.add_argument("--remote-root", default="/live-sentinel")
    parser.add_argument("--timeout-sec", type=float, default=3600)
    args = parser.parse_args()
    session_dir = args.session_dir.expanduser().resolve()
    session_id = args.session_id or session_dir.name
    result = BaiduNetdiskBackup(
        command=args.command,
        remote_root=args.remote_root,
        timeout_sec=args.timeout_sec,
        required=True,
    ).backup(session_dir, session_id)
    print(f"{result.provider}: {result.status} -> {result.remote_path}")
    if result.detail:
        print(result.detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
