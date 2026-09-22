"""通知接口、本地实现、飞书机器人 Webhook 和 ntfy 推送。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Sequence
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..models import Highlight, InterestEvent


class Notifier(Protocol):
    def send(self, event: InterestEvent, highlight: Highlight | None = None) -> None:
        ...


class MemoryNotifier:
    """测试用通知器；生产环境可替换为桌面、Webhook 或消息机器人。"""

    def __init__(self):
        self.events: list[tuple[InterestEvent, Highlight | None]] = []

    def send(self, event: InterestEvent, highlight: Highlight | None = None) -> None:
        self.events.append((event, highlight))


class ConsoleNotifier:
    def send(self, event: InterestEvent, highlight: Highlight | None = None) -> None:
        topic = highlight.topic if highlight else event.payload.get("topic", "")
        print(f"🔥 检测到高价值内容 | {topic} | {event.timestamp_ms}ms | score={event.score:.2f}")


def _format_time(timestamp_ms: int) -> str:
    total_seconds = max(0, timestamp_ms // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _notification_text(event: InterestEvent, highlight: Highlight | None) -> str:
    topic = (highlight.topic if highlight else event.payload.get("topic", "")) or "未命名主题"
    summary = (
        highlight.summary
        if highlight and highlight.summary
        else str(event.payload.get("summary", ""))
    )
    lines = [
        "🔥 检测到高价值内容",
        f"主题：{topic}",
        f"摘要：{summary or '暂无摘要'}",
        f"时间：{_format_time(event.timestamp_ms)}",
        f"评分：{event.score:.2f}",
    ]
    if highlight and highlight.session_id:
        lines.append(f"Session：{highlight.session_id}")
    return "\n".join(lines)


class FeishuWebhookNotifier:
    """发送飞书自定义机器人文本消息。

    ``secret`` 非空时按飞书机器人签名规则附加 ``timestamp`` 和 ``sign``；不传
    secret 则使用 Webhook 自身的 key 鉴权。请求失败会抛出异常，由上层记录为
    ``NOTIFICATION_ERROR``，不会影响音频归档。
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        secret: str | None = None,
        timeout_sec: float = 10.0,
    ):
        if not webhook_url:
            raise ValueError("飞书 Webhook URL 不能为空")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec 必须为正数")
        self.webhook_url = webhook_url
        self.secret = secret or None
        self.timeout_sec = timeout_sec

    @staticmethod
    def _format_time(timestamp_ms: int) -> str:
        return _format_time(timestamp_ms)

    def _payload(self, event: InterestEvent, highlight: Highlight | None) -> dict[str, object]:
        payload: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": _notification_text(event, highlight)},
        }
        if self.secret:
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{self.secret}"
            digest = hmac.new(
                string_to_sign.encode("utf-8"),
                b"",
                hashlib.sha256,
            ).digest()
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(digest).decode("ascii")
        return payload

    def _post_payload(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_sec) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"飞书 Webhook HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"飞书 Webhook 网络请求失败: {exc.reason}") from exc
        if not raw:
            return
        try:
            response_payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(response_payload, dict):
            return
        code = response_payload.get("code", response_payload.get("StatusCode"))
        if code not in (None, 0, "0"):
            message = response_payload.get(
                "msg", response_payload.get("StatusMessage", "unknown error")
            )
            raise RuntimeError(f"飞书 Webhook 返回错误 {code}: {message}")

    def send(self, event: InterestEvent, highlight: Highlight | None = None) -> None:
        self._post_payload(self._payload(event, highlight))

    def send_text(self, text: str) -> None:
        """发送普通文本，用于单场完成摘要等非高价值事件。"""

        if not text:
            raise ValueError("飞书文本消息不能为空")
        payload: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        if self.secret:
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{self.secret}"
            digest = hmac.new(
                string_to_sign.encode("utf-8"), b"", hashlib.sha256
            ).digest()
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(digest).decode("ascii")
        self._post_payload(payload)


class NtfyNotifier:
    """通过 ntfy 的 HTTP publish endpoint 发送文本推送。

    ``publish_url`` 是完整的发布地址，例如
    ``https://ntfy.sh/<随机 topic>`` 或自托管服务的
    ``https://ntfy.example.com/<topic>``。不把 topic 拼接到代码中，便于使用
    私有服务器、随机 topic 或受保护的发布地址。``token`` 可选；公开 topic
    不需要 token，自托管且开启认证时可通过 ``Authorization: Bearer`` 传入。

    ntfy 的标题、优先级、标签和点击链接使用 HTTP headers 传递，正文保留
    中文事件摘要。请求失败会抛出异常，由上层记录为 ``NOTIFICATION_ERROR``，
    不会影响 P0 音频归档。
    """

    _VALID_PRIORITIES = frozenset(
        {"min", "low", "default", "high", "max", "1", "2", "3", "4", "5"}
    )

    def __init__(
        self,
        publish_url: str,
        *,
        token: str | None = None,
        title: str = "Live Sentinel",
        priority: str = "high",
        tags: Sequence[str] | None = None,
        click_url: str | None = None,
        timeout_sec: float = 10.0,
    ):
        if not publish_url:
            raise ValueError("ntfy publish URL 不能为空")
        if not title:
            raise ValueError("ntfy title 不能为空")
        try:
            # urllib encodes request headers as ISO-8859-1. Keep the default and
            # configured title representable instead of failing late in urlopen.
            title.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError("ntfy title 必须使用 ASCII/Latin-1 字符") from exc
        if timeout_sec <= 0:
            raise ValueError("timeout_sec 必须为正数")
        normalized_priority = str(priority).strip().lower()
        if normalized_priority not in self._VALID_PRIORITIES:
            allowed = ", ".join(sorted(self._VALID_PRIORITIES))
            raise ValueError(f"ntfy priority 无效: {priority!r}；可选值: {allowed}")
        if isinstance(tags, str):
            normalized_tags = (tags,)
        else:
            normalized_tags = tuple(tags or ())
        if any(not isinstance(tag, str) or not tag.strip() for tag in normalized_tags):
            raise ValueError("ntfy tags 必须是非空字符串列表")
        if click_url is not None and not isinstance(click_url, str):
            raise ValueError("ntfy click_url 必须是字符串或 null")
        self.publish_url = publish_url
        self.token = token or None
        self.title = title
        self.priority = normalized_priority
        self.tags = normalized_tags
        self.click_url = click_url or None
        self.timeout_sec = timeout_sec

    def _headers(self, event: InterestEvent | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "text/plain; charset=utf-8",
            "Title": self.title,
            "Priority": self.priority,
        }
        if self.tags:
            headers["Tags"] = ",".join(self.tags)
        click_url = (event.payload.get("click_url") if event is not None else None) or self.click_url
        if click_url:
            headers["Click"] = str(click_url)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def send(self, event: InterestEvent, highlight: Highlight | None = None) -> None:
        body = _notification_text(event, highlight).encode("utf-8")
        request = Request(
            self.publish_url,
            data=body,
            headers=self._headers(event),
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_sec) as response:
                response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"ntfy HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"ntfy 网络请求失败: {exc.reason}") from exc

    def send_text(self, text: str) -> None:
        """Send a plain-text session summary through the configured ntfy topic."""

        if not text:
            raise ValueError("ntfy 文本消息不能为空")
        request = Request(
            self.publish_url,
            data=text.encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_sec) as response:
                response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"ntfy HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"ntfy 网络请求失败: {exc.reason}") from exc
