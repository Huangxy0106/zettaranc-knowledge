#!/usr/bin/env python3
"""把 B 站 Cookie 安全写入 Live Sentinel 的本机凭据文件。"""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path


REQUIRED_COOKIE_NAMES = {"SESSDATA", "DedeUserID", "bili_jct"}
MINIMAL_COOKIE_NAMES = {
    "SESSDATA",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "bili_jct",
    "bili_ticket",
    "buvid3",
    "buvid4",
    "buvid_fp",
    "LIVE_BUVID",
}


def _cookie_names(value: str) -> set[str]:
    return {
        item.split("=", 1)[0].strip()
        for item in value.split(";")
        if "=" in item and item.split("=", 1)[0].strip()
    }


def import_cookie(env_file: str | Path, value: str) -> set[str]:
    cookie = value.strip()
    if not cookie or "\r" in cookie or "\n" in cookie:
        raise ValueError("Cookie 不能为空或包含换行符")
    names = _cookie_names(cookie)
    missing = REQUIRED_COOKIE_NAMES - names
    if missing:
        raise ValueError(f"Cookie 缺少必要字段: {', '.join(sorted(missing))}")

    destination = Path(env_file).expanduser().resolve()
    old_lines = (
        destination.read_text(encoding="utf-8").splitlines()
        if destination.exists()
        else []
    )
    new_lines = [line for line in old_lines if not line.startswith("BILIBILI_COOKIE=")]
    new_lines.append(f"BILIBILI_COOKIE={cookie}")
    temporary = destination.with_name(destination.name + ".new")
    temporary.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
    return names


def cookie_from_edge() -> str:
    try:
        import browser_cookie3
    except ImportError as exc:
        raise RuntimeError(
            "从 Edge 导入需要安装 browser-cookie3（pip install -e '.[credentials]'）"
        ) from exc
    jar = browser_cookie3.edge(domain_name=".bilibili.com")
    selected = {
        cookie.name: cookie.value
        for cookie in jar
        if cookie.name in MINIMAL_COOKIE_NAMES and cookie.value
    }
    missing = REQUIRED_COOKIE_NAMES - selected.keys()
    if missing:
        raise RuntimeError(f"Edge 中缺少 B 站登录字段: {', '.join(sorted(missing))}")
    return "; ".join(f"{name}={selected[name]}" for name in sorted(selected))


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 B 站登录 Cookie（输入不会回显）")
    parser.add_argument(
        "--env-file",
        default=".live-sentinel.env",
        help="Live Sentinel 本机凭据文件",
    )
    parser.add_argument(
        "--from-edge",
        action="store_true",
        help="只读导入 Edge 中 bilibili.com 的最小 Cookie 集",
    )
    args = parser.parse_args()
    cookie = (
        cookie_from_edge()
        if args.from_edge
        else getpass.getpass("请粘贴 B 站 Cookie（输入不回显）: ")
    )
    names = import_cookie(args.env_file, cookie)
    print(f"已保存 B 站 Cookie（{len(names)} 个字段），文件权限为 0600。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
