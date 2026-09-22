#!/usr/bin/env python3
"""向手机 ntfy topic 发送一条明确的连调消息。

用法：
    NTFY_URL='https://ntfy.sh/<随机topic>' python3 scripts/test_ntfy.py

也可以通过 ``--url`` / ``--token`` 临时覆盖环境变量。脚本不会打印 token。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_sentinel.models import Highlight, InterestEvent, InterestState
from live_sentinel.notification.notifier import NtfyNotifier


def main() -> int:
    parser = argparse.ArgumentParser(description="发送 ntfy 手机连调通知")
    parser.add_argument("--url", default=os.environ.get("NTFY_URL"), help="完整 publish URL")
    parser.add_argument("--token", default=os.environ.get("NTFY_TOKEN"), help="可选 Bearer token")
    parser.add_argument("--title", default="Live Sentinel test", help="ASCII 标题")
    args = parser.parse_args()
    if not args.url:
        parser.error("请设置 NTFY_URL 或传入 --url")
    notifier = NtfyNotifier(
        args.url,
        token=args.token,
        title=args.title,
        priority="high",
        tags=["test", "live_sentinel"],
    )
    event = InterestEvent(
        "TEST_NOTIFICATION",
        12_345,
        InterestState.HOT,
        0.99,
        payload={
            "topic": "ntfy 连调",
            "summary": "如果手机收到这条消息，Live Sentinel 的 ntfy 出口已接通。",
        },
    )
    notifier.send(event, Highlight("test", "ntfy-test", 12_000, "ntfy", 0.99, "连调消息"))
    print("ntfy publish 请求已成功返回；请在手机应用确认收到通知。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
