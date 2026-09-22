"""通过用户已登录的桌面浏览器打开获授权的 B 站直播页。"""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
from urllib.parse import urlsplit


@dataclass
class BrowserPlaybackController:
    """只负责打开直播页；是否确有声音由音频输入探针独立验真。

    不尝试导出或处理 DRM 密钥，也不在结束时粗暴关闭浏览器或用户已有标签页。
    """

    app_name: str
    url: str
    room_ids: tuple[str, ...] = ()
    open_command: str = "open"
    script_command: str = "osascript"
    background: bool = False
    timeout_sec: float = 15.0

    def _validated_room_ids(self) -> tuple[str, ...]:
        path_room = urlsplit(self.url).path.strip("/")
        candidates = dict.fromkeys((*self.room_ids, path_room))
        if not candidates or any(not value.isdigit() for value in candidates):
            raise ValueError("直播间 ID 必须为数字")
        return tuple(candidates)

    @staticmethod
    def _edge_reuse_script() -> str:
        # All dynamic values are passed in argv. Keeping them out of the script
        # prevents URL/application text from becoming AppleScript source.
        return """
on run argv
    set targetURL to item 1 of argv
    set shouldActivate to item 2 of argv
    set roomIDs to items 3 thru -1 of argv
    tell application "Microsoft Edge"
        repeat with currentWindow in windows
            set tabCount to count of tabs of currentWindow
            repeat with tabIndex from 1 to tabCount
                set currentTab to tab tabIndex of currentWindow
                set currentURL to URL of currentTab
                repeat with roomID in roomIDs
                    set roomBase to "https://live.bilibili.com/" & roomID
                    if currentURL is roomBase or currentURL starts with (roomBase & "?") or currentURL starts with (roomBase & "#") or currentURL starts with (roomBase & "/") then
                        set active tab index of currentWindow to tabIndex
                        set URL of currentTab to targetURL
                        if shouldActivate is "true" then activate
                        return "reused"
                    end if
                end repeat
            end repeat
        end repeat
        if (count of windows) is 0 then
            make new window
            set URL of active tab of front window to targetURL
        else
            tell front window
                make new tab at end of tabs with properties {URL:targetURL}
                set active tab index to count of tabs
            end tell
        end if
        if shouldActivate is "true" then activate
        return "opened"
    end tell
end run
""".strip()

    def _reuse_or_open_edge(self, room_ids: tuple[str, ...]) -> str | None:
        if self.app_name.casefold() not in {"microsoft edge", "edge"}:
            return None
        if shutil.which(self.script_command) is None:
            return None
        completed = subprocess.run(
            [
                self.script_command,
                "-e",
                self._edge_reuse_script(),
                self.url,
                "false" if self.background else "true",
                *room_ids,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_sec,
        )
        if completed.returncode != 0:
            return None
        result = completed.stdout.strip()
        return result if result in {"reused", "opened"} else None

    def start(self) -> str:
        if not self.app_name or "\n" in self.app_name or "\r" in self.app_name:
            raise ValueError("浏览器应用名称无效")
        if not self.url.startswith("https://live.bilibili.com/"):
            raise ValueError("自动播放控制器只允许打开 B 站直播页")
        room_ids = self._validated_room_ids()
        reused = self._reuse_or_open_edge(room_ids)
        if reused is not None:
            return reused
        if shutil.which(self.open_command) is None:
            raise RuntimeError(f"找不到浏览器启动命令: {self.open_command}")
        command = [self.open_command]
        if self.background:
            command.append("-g")
        command.extend(["-a", self.app_name, self.url])
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_sec,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(
                f"打开授权直播页失败，returncode={completed.returncode}: {detail}"
            )
        return "opened_fallback"
