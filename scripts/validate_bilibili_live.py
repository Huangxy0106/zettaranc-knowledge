#!/usr/bin/env python3
"""对一个公开可播放的 B 站房间做限时、本地归档验证。"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_sentinel.bilibili import fetch_room_info
from live_sentinel.config import AppConfig, load_config
from live_sentinel.integrations import build_integrations
from live_sentinel.runner import run_bilibili_session


def _config(config_path: str | Path | None = None) -> AppConfig:
    return load_config(config_path) if config_path is not None else AppConfig()


def run_validation(
    url: str,
    output_dir: str | Path | None,
    duration_sec: int,
    *,
    config_path: str | Path | None = None,
    strict_integrations: bool = False,
) -> int:
    info = fetch_room_info(url, include_stream=True)
    print(
        f"房间 {info.room_id} | 主播 {info.uname} | "
        f"直播状态 {'LIVE' if info.is_live else 'OFFLINE'} | 标题 {info.title}"
    )
    if not info.is_live:
        print("直播当前不在线，未启动采集。")
        return 2

    config = _config(config_path)
    integrations = build_integrations(config, strict=strict_integrations)
    print(
        "适配器 | "
        f"realtime={integrations.realtime_name} | "
        f"offline={integrations.offline_name} | "
        f"llm={integrations.llm_name} | "
        f"notify={integrations.notifier_name} | "
        f"summary={integrations.summary_notifier_name} | "
        f"feishu_docs={integrations.feishu_docs_name} | "
        f"backup={integrations.backup_name}"
    )
    run = run_bilibili_session(
        info,
        config,
        output_dir=output_dir,
        max_duration_sec=duration_sec,
        strict_integrations=strict_integrations,
        integrations=integrations,
    )
    session = run.result.session
    print(
        f"验证结束 | 状态 {session.status.value} | 实际时长 {session.duration_ms / 1000:.1f}s | "
        f"实时转写 {len(run.result.realtime_transcripts)} 条"
    )
    if run.result.final_audio is None:
        print(f"未生成 Final Audio，请检查网络/播放权限。诊断目录: {run.root}")
        return 3
    print(f"Final Audio: {run.result.final_audio.file_path}")
    print(f"诊断目录: {run.root}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="B站直播限时本地归档验证")
    parser.add_argument("url", help="B站直播页 URL、room_id 或短号")
    parser.add_argument("--duration-sec", type=int, default=300, help="验证时长，默认 300 秒")
    parser.add_argument(
        "--output-dir",
        help="输出根目录；省略时使用配置 storage.root_dir（默认 sessions）",
    )
    parser.add_argument("--config", help="JSON/YAML 配置文件；默认使用内置配置")
    parser.add_argument(
        "--strict-integrations",
        action="store_true",
        help="要求配置的外部适配器凭据全部存在，否则启动失败",
    )
    args = parser.parse_args()
    if args.duration_sec <= 0:
        parser.error("--duration-sec 必须为正数")
    try:
        return run_validation(
            args.url,
            args.output_dir,
            args.duration_sec,
            config_path=args.config,
            strict_integrations=args.strict_integrations,
        )
    except (OSError, socket.timeout, RuntimeError, ValueError) as exc:
        print(f"验证失败: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
