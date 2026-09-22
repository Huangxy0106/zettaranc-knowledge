"""百度网盘备份适配器。

百度个人网盘没有一个稳定、免交互的通用上传 API 可直接假定。这里使用社区维护
的 ``bypy`` CLI 作为可替换执行器：首次授权由用户在终端完成，系统之后只负责
上传已经落到 T5 的 Session 目录。若 ``bypy`` 不存在，录音仍完成，备份状态会被
记录为 unavailable/error，而不会把主归档判成失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True)
class BackupResult:
    provider: str
    status: str
    local_path: str
    remote_path: str
    detail: str = ""


class BaiduNetdiskBackup:
    """用 ``bypy upload LOCAL_DIR REMOTE_DIR`` 上传一个完整 Session 目录。"""

    def __init__(
        self,
        *,
        command: str = "bypy",
        remote_root: str = "/live-sentinel",
        timeout_sec: float = 3_600.0,
        required: bool = False,
    ):
        if not command:
            raise ValueError("bypy command 不能为空")
        if not remote_root:
            raise ValueError("remote_root 不能为空")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec 必须为正数")
        self.command = command
        self.remote_root = remote_root.rstrip("/") or "/"
        self.timeout_sec = timeout_sec
        self.required = required

    def available(self) -> bool:
        return shutil.which(self.command) is not None

    def backup(self, session_root: str | Path, session_id: str) -> BackupResult:
        local = Path(session_root).expanduser().resolve()
        if not local.exists() or not local.is_dir():
            raise FileNotFoundError(f"Session 目录不存在: {local}")
        remote = f"{self.remote_root}/{session_id}"
        if not self.available():
            detail = f"找不到 {self.command}；先安装 bypy 并完成一次授权"
            if self.required:
                raise RuntimeError(detail)
            return BackupResult("baidu_bypy", "unavailable", str(local), remote, detail)
        command = [self.command, "upload", str(local), remote]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"百度网盘备份超时: {self.timeout_sec:.0f}s") from exc
        detail = (completed.stdout + "\n" + completed.stderr).strip()[-2_000:]
        if completed.returncode != 0:
            raise RuntimeError(f"百度网盘备份失败，退出码 {completed.returncode}: {detail}")
        return BackupResult("baidu_bypy", "uploaded", str(local), remote, detail)
