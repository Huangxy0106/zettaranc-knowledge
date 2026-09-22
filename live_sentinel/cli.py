"""Live Sentinel Watchlist 命令行入口。"""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import sys

from .bilibili import fetch_room_info
from .config import AppConfig, load_config
from .integrations import build_notifier, build_summary_notifier
from .launchd import bootstrap, service_status, uninstall, write_plist
from .observability import configure_service_logging, redact_text
from .watchlist.store import WatchlistStore
from .watchlist.supervisor import WatchlistSupervisor


def _load_env_file(path: str | Path | None) -> None:
    if path is None:
        return
    env_path = Path(path).expanduser().resolve()
    for number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"环境文件第 {number} 行缺少 '='")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "a").isalnum():
            raise ValueError(f"环境文件第 {number} 行变量名无效")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _config(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.config) if args.config else AppConfig()
    if args.state_db:
        config.watchlist.state_db = args.state_db
    return config


def _store(config: AppConfig) -> WatchlistStore:
    return WatchlistStore(config.watchlist.state_db)


def _watchlist_alert(config: AppConfig):
    notifier, _ = build_summary_notifier(config, strict=False)
    if notifier is None:
        fallback, _ = build_notifier(config, strict=False)
        notifier = fallback if callable(getattr(fallback, "send_text", None)) else None
    return getattr(notifier, "send_text", None) if notifier is not None else None


def _print_sources(store: WatchlistStore) -> None:
    sources = store.list_sources()
    if not sources:
        print("Watchlist 为空。使用 sentinel source add <房间 URL> 添加。")
        return
    header = f"{'ID':>3}  {'EN':<3} {'SOURCE':<22} {'STATE':<11} {'SESSION':<11} NAME"
    print(header)
    print("-" * len(header))
    for source in sources:
        session = source.last_session_status or "-"
        source_id = source.requested_room_id
        if source.requested_room_id != source.room_id:
            source_id = f"{source.requested_room_id}→{source.room_id}"
        print(
            f"{source.id:>3}  {'yes' if source.enabled else 'no':<3} "
            f"{source_id:<22} {source.observed_state:<11} {session:<11} {source.name}"
        )
        if source.creator_uid:
            print(f"     uid: {source.creator_uid}")
        if source.last_error:
            print(f"     error: {source.last_error}")


def _service_lock(store: WatchlistStore):
    lock_path = store.path.with_suffix(store.path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("已有 Watchlist 服务在运行") from exc
    return handle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel", description="Live Sentinel Watchlist")
    parser.add_argument("--config", help="JSON/YAML 运行配置")
    parser.add_argument("--env-file", help="仅本机使用的 KEY=VALUE 凭据文件")
    parser.add_argument("--state-db", help="覆盖 watchlist.state_db")
    commands = parser.add_subparsers(dest="command", required=True)

    source = commands.add_parser("source", help="管理关注源")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    add = source_commands.add_parser("add", help="添加或更新一个 B 站关注源")
    add.add_argument("reference", help="room_id、短号或数字直播页 URL")
    add.add_argument("--name", help="显示名称；默认使用公开主播名")
    add.add_argument("--uid", help="主播 UID（用于身份审计，不参与房间去重）")
    add.add_argument("--profile", default="default", help="内容策略 profile")
    add.add_argument("--disabled", action="store_true", help="保存但暂不启用")
    source_commands.add_parser("list", help="列出关注源")
    enable = source_commands.add_parser("enable", help="启用关注源")
    enable.add_argument("selector", help="source ID、room_id 或精确名称")
    disable = source_commands.add_parser("disable", help="暂停关注源")
    disable.add_argument("selector", help="source ID、room_id 或精确名称")

    commands.add_parser("status", help="查看 Watchlist 状态")
    service = commands.add_parser("service", help="运行或安装后台服务")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    run = service_commands.add_parser("run", help="前台运行调度器（launchd 使用）")
    run.add_argument("--once", action="store_true", help="只执行一次发现循环")
    install = service_commands.add_parser("install", help="安装并启动 launchd 用户服务")
    install.add_argument("--no-start", action="store_true", help="只写 plist，不立即加载")
    service_commands.add_parser("status", help="查看 launchd 服务状态")
    service_commands.add_parser("uninstall", help="停止并移除 launchd 用户服务")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _load_env_file(args.env_file)
        config = _config(args)
        if args.command == "source":
            store = _store(config)
            try:
                if args.source_command == "add":
                    info = fetch_room_info(
                        args.reference,
                        include_stream=False,
                        timeout=config.watchlist.discovery_timeout_sec,
                    )
                    if args.uid and info.creator_uid and args.uid != info.creator_uid:
                        raise ValueError(
                            f"主播 UID 不匹配: 输入 {args.uid}, B 站公开接口返回 {info.creator_uid}"
                        )
                    saved = store.add_source(
                        room_id=str(info.room_id),
                        requested_room_id=str(info.requested_room_id),
                        creator_uid=args.uid or info.creator_uid,
                        url=info.url,
                        name=args.name or info.uname,
                        profile=args.profile,
                        enabled=not args.disabled,
                    )
                    print(
                        f"已保存 source {saved.id}: {saved.name} | room {saved.room_id} | "
                        f"{'enabled' if saved.enabled else 'disabled'}"
                    )
                elif args.source_command == "list":
                    _print_sources(store)
                else:
                    enabled = args.source_command == "enable"
                    saved = store.set_enabled(args.selector, enabled)
                    print(f"{saved.name}: {'enabled' if saved.enabled else 'disabled'}")
            finally:
                store.close()
            return 0
        if args.command == "status":
            store = _store(config)
            try:
                _print_sources(store)
                loaded, state = service_status()
                print(f"launchd: {'loaded' if loaded else 'not loaded'} ({state})")
            finally:
                store.close()
            return 0
        if args.command == "service":
            if args.service_command == "install":
                path = write_plist(
                    config_path=args.config,
                    env_file=args.env_file,
                    state_db=args.state_db,
                    working_directory=Path(__file__).resolve().parents[1],
                )
                if not args.no_start:
                    bootstrap(path)
                print(f"launchd plist: {path}")
                return 0
            if args.service_command == "status":
                loaded, state = service_status()
                print(f"{'loaded' if loaded else 'not loaded'}: {state}")
                return 0 if loaded else 1
            if args.service_command == "uninstall":
                print("已移除" if uninstall() else "未安装")
                return 0
            store = _store(config)
            lock = _service_lock(store)
            try:
                configure_service_logging(
                    Path.home() / "Library" / "Logs" / "LiveSentinel" / "watchlist.err.log"
                )
                supervisor = WatchlistSupervisor(
                    config,
                    store,
                    alert=_watchlist_alert(config),
                )
                if args.once:
                    supervisor.recover_interrupted_sessions()
                    supervisor.run_once()
                else:
                    supervisor.run_forever()
            finally:
                lock.close()
                store.close()
            return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"sentinel: {redact_text(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
