"""可恢复运行所需的轻量 checkpoint。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Checkpoint:
    session_id: str
    last_timestamp_ms: int
    last_segment_id: str | None = None
    updated_at: str = ""

    def with_timestamp(self, timestamp_ms: int, segment_id: str | None = None) -> "Checkpoint":
        return Checkpoint(
            session_id=self.session_id,
            last_timestamp_ms=timestamp_ms,
            last_segment_id=segment_id if segment_id is not None else self.last_segment_id,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )


class CheckpointStore:
    """使用同目录临时文件 + replace，避免写入过程中留下半个 JSON。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, checkpoint: Checkpoint) -> None:
        payload = asdict(checkpoint)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            Path(temp_name).unlink(missing_ok=True)

    def load(self) -> Checkpoint | None:
        if not self.path.exists():
            return None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return Checkpoint(**payload)
