"""macOS 系统音频输出的受控切换。"""

from __future__ import annotations

import logging
import shutil
import subprocess


LOGGER = logging.getLogger("live_sentinel.audio.route")


class MacOSAudioOutputRoute:
    """把系统输出切到指定设备并保持该路由。

    Live Sentinel 采用 fail-safe 的无物理外放策略：Session 结束后不自动
    恢复扬声器。需要恢复时应由用户明确切换输出设备。
    """

    def __init__(
        self,
        device: str,
        *,
        command: str = "SwitchAudioSource",
    ):
        if not device or "\r" in device or "\n" in device:
            raise ValueError("音频输出设备名称无效")
        self.device = device
        self.command = command

    def _run(self, *arguments: str) -> str:
        result = subprocess.run(
            [self.command, *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()

    def activate(self) -> str:
        if shutil.which(self.command) is None:
            raise RuntimeError(
                "找不到 SwitchAudioSource；请先安装 switchaudio-osx"
            )
        current = self._run("-c", "-t", "output")
        available = {
            line.strip()
            for line in self._run("-a", "-t", "output").splitlines()
            if line.strip()
        }
        if self.device not in available:
            raise RuntimeError(f"找不到系统音频输出设备: {self.device}")
        if current != self.device:
            self._run("-s", self.device, "-t", "output")
            observed = self._run("-c", "-t", "output")
            if observed != self.device:
                raise RuntimeError(
                    f"系统音频输出切换验证失败: expected={self.device}, observed={observed}"
                )
            LOGGER.info("System audio output routed to %s", self.device)
        return current
