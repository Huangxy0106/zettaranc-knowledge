"""把已完成 Session 从本地 staging 安全提升到主归档卷。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class PromotionResult:
    source_root: Path
    archive_root: Path
    file_count: int
    total_bytes: int
    manifest_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(root: Path, *, with_hashes: bool) -> dict[str, tuple[int, str | None]]:
    result: dict[str, tuple[int, str | None]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        result[relative] = (path.stat().st_size, _sha256(path) if with_hashes else None)
    return result


def _replace_strings(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace_strings(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_strings(item, old, new) for key, item in value.items()}
    return value


def _rebase_metadata(
    root: Path,
    source_roots: tuple[Path, ...],
    archive_root: Path,
) -> None:
    new = str(archive_root)
    for relative in ("session.json", "final/session.json", "backup.json"):
        path = root / relative
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for source_root in source_roots:
            payload = _replace_strings(payload, str(source_root), new)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    database = root / "session.sqlite"
    if database.exists():
        connection = sqlite3.connect(database)
        try:
            with connection:
                for source_root in source_roots:
                    old = str(source_root)
                    connection.execute(
                        "UPDATE sessions SET final_audio_path = replace(final_audio_path, ?, ?)",
                        (old, new),
                    )
                    connection.execute(
                        "UPDATE audio_segments SET file_path = replace(file_path, ?, ?)",
                        (old, new),
                    )
                    connection.execute(
                        "UPDATE audio_final SET file_path = replace(file_path, ?, ?)",
                        (old, new),
                    )
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise RuntimeError(f"迁移后的 SQLite 完整性检查失败: {row}")
        finally:
            connection.close()


def promote_session(
    source_root: str | Path,
    archive_root: str | Path,
    *,
    verify_sha256: bool = True,
) -> PromotionResult:
    """校验复制后原子发布 Session；成功发布前始终保留 staging 源。"""

    source_argument = Path(source_root).expanduser().absolute()
    source = source_argument.resolve()
    destination = Path(archive_root).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"staging Session 不存在: {source}")
    if destination.exists():
        raise FileExistsError(f"主归档 Session 已存在，拒绝覆盖: {destination}")
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("staging 与主归档 Session 目录不得互相包含")

    destination.parent.mkdir(parents=True, exist_ok=True)
    incoming = destination.parent / f".{destination.name}.incoming-{uuid4().hex}"
    source_manifest = _manifest(source, with_hashes=verify_sha256)
    try:
        shutil.copytree(source, incoming, copy_function=shutil.copy2)
        copied_manifest = _manifest(incoming, with_hashes=verify_sha256)
        if source_manifest != copied_manifest:
            raise RuntimeError("staging 到主归档的文件清单或校验和不一致")
        source_aliases = tuple(dict.fromkeys((source_argument, source)))
        _rebase_metadata(incoming, source_aliases, destination)
        archive_manifest = _manifest(incoming, with_hashes=verify_sha256)

        manifest_payload = {
            "source_root": str(source),
            "archive_root": str(destination),
            "file_count": len(archive_manifest),
            "total_bytes": sum(size for size, _ in archive_manifest.values()),
            "source_copy_verified": True,
            "verified_with_sha256": verify_sha256,
            "files": {
                name: {"size": size, "sha256": checksum}
                for name, (size, checksum) in archive_manifest.items()
            },
        }
        manifest_text = json.dumps(manifest_payload, ensure_ascii=False, indent=2)
        promotion_path = incoming / "promotion.json"
        promotion_path.write_text(manifest_text, encoding="utf-8")
        promotion_path.chmod(0o600)
        manifest_sha256 = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()

        # incoming 和 destination 在同一个 T5 文件系统中；这里的 rename 是原子的。
        os.replace(incoming, destination)
        shutil.rmtree(source)
        return PromotionResult(
            source_root=source,
            archive_root=destination,
            file_count=len(archive_manifest),
            total_bytes=sum(size for size, _ in archive_manifest.values()),
            manifest_sha256=manifest_sha256,
        )
    except Exception:
        if incoming.exists():
            shutil.rmtree(incoming)
        raise
