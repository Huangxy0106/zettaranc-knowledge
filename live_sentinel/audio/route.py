"""macOS 系统音频输出的受控切换。"""

from __future__ import annotations

import logging
import shutil
import subprocess


LOGGER = logging.getLogger("live_sentinel.audio.route")
BLACKHOLE_DEVICE = "BlackHole 2ch"
BLACKHOLE_OUTPUT_VOLUME = 100


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
        volume_command: str = "osascript",
    ):
        if not device or "\r" in device or "\n" in device:
            raise ValueError("音频输出设备名称无效")
        self.device = device
        self.command = command
        self.volume_command = volume_command

    def _run(self, *arguments: str) -> str:
        result = subprocess.run(
            [self.command, *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()

    def _set_blackhole_volume(self, observed_device: str) -> None:
        """只在当前输出已验证为 BlackHole 时提高虚拟设备音量。"""

        if self.device != BLACKHOLE_DEVICE or observed_device != BLACKHOLE_DEVICE:
            return
        if shutil.which(self.volume_command) is None:
            raise RuntimeError("找不到 osascript，无法安全设置 BlackHole 输出音量")
        subprocess.run(
            [
                self.volume_command,
                "-e",
                f"set volume output volume {BLACKHOLE_OUTPUT_VOLUME} without output muted",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # AppleScript adjusts the current output device, so verify that no
        # concurrent route change occurred and that the requested value stuck.
        after_device = self._run("-c", "-t", "output")
        if after_device != BLACKHOLE_DEVICE:
            raise RuntimeError(
                "设置音量期间系统输出设备发生变化: "
                f"expected={BLACKHOLE_DEVICE}, observed={after_device}"
            )
        volume = subprocess.run(
            [
                self.volume_command,
                "-e",
                "output volume of (get volume settings)",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if volume != str(BLACKHOLE_OUTPUT_VOLUME):
            raise RuntimeError(
                "BlackHole 输出音量设置验证失败: "
                f"expected={BLACKHOLE_OUTPUT_VOLUME}, observed={volume}"
            )
        LOGGER.info(
            "System output volume set to %d%% after confirming %s",
            BLACKHOLE_OUTPUT_VOLUME,
            BLACKHOLE_DEVICE,
        )

    def activate(self) -> str:
        if shutil.which(self.command) is None:
            raise RuntimeError(
                "找不到 SwitchAudioSource；请先安装 switchaudio-osx"
            )
        previous = self._run("-c", "-t", "output")
        observed = previous
        available = {
            line.strip()
            for line in self._run("-a", "-t", "output").splitlines()
            if line.strip()
        }
        if self.device not in available:
            raise RuntimeError(f"找不到系统音频输出设备: {self.device}")
        if previous != self.device:
            self._run("-s", self.device, "-t", "output")
            observed = self._run("-c", "-t", "output")
            if observed != self.device:
                raise RuntimeError(
                    f"系统音频输出切换验证失败: expected={self.device}, observed={observed}"
                )
            LOGGER.info("System audio output routed to %s", self.device)
        self._set_blackhole_volume(observed)
        # Return the route that was active before this Session for audit only;
        # policy intentionally does not restore it after capture.
        return previous
