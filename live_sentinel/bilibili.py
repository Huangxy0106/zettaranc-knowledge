"""B 站公开直播状态与播放地址发现。

这里只读取公开房间接口，不读取登录凭据，也不尝试绕过付费、地域或 DRM 限制。
状态发现和播放地址解析分开，避免离线轮询时请求无用的播放地址。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
)


@dataclass(frozen=True)
class BilibiliRoomInfo:
    room_id: int
    requested_room_id: int
    creator_uid: str | None
    uname: str
    title: str
    live_status: int
    stream_url: str | None = None
    stream_drm: bool = False

    @property
    def is_live(self) -> bool:
        return self.live_status == 1

    @property
    def url(self) -> str:
        return f"https://live.bilibili.com/{self.room_id}"


def room_id_from_reference(reference: str | int) -> int:
    """接受 room_id、短号或直播页 URL，返回其中的数字 ID。"""

    if isinstance(reference, int):
        if reference <= 0:
            raise ValueError("B 站房间 ID 必须为正整数")
        return reference
    value = reference.strip()
    if value.isdigit() and int(value) > 0:
        return int(value)
    path = urlsplit(value).path.rstrip("/")
    candidate = path.rsplit("/", 1)[-1]
    if candidate.isdigit() and int(candidate) > 0:
        return int(candidate)
    raise ValueError("请输入 B 站直播 room_id、短号或数字房间 URL")


def _request_headers() -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://live.bilibili.com/",
        "Cache-Control": "no-cache",
    }
    cookie = os.environ.get("BILIBILI_COOKIE", "").strip()
    if "\r" in cookie or "\n" in cookie:
        raise RuntimeError("BILIBILI_COOKIE 不能包含换行符")
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _fetch_text(url: str, timeout: int = 20) -> str:
    request = Request(
        url,
        headers=_request_headers(),
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _fetch_json(url: str, timeout: int = 20) -> dict[str, object]:
    payload = json.loads(_fetch_text(url, timeout=timeout))
    if not isinstance(payload, dict):
        raise RuntimeError("B 站接口返回不是 JSON 对象")
    return payload


@dataclass(frozen=True)
class BilibiliStreamInfo:
    url: str
    drm: bool = False


def _select_stream_info(playurl: object) -> BilibiliStreamInfo:
    streams = playurl.get("stream") if isinstance(playurl, dict) else None
    if not isinstance(streams, list):
        raise RuntimeError("B 站播放地址接口没有 stream")

    candidates: list[tuple[int, int, int, str]] = []
    transport_priority = {
        ("http_stream", "flv"): 0,
        ("http_hls", "fmp4"): 1,
        ("http_hls", "ts"): 2,
    }
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        protocol_name = stream.get("protocol_name")
        formats = stream.get("format")
        if not isinstance(protocol_name, str) or not isinstance(formats, list):
            continue
        for media_format in formats:
            if not isinstance(media_format, dict):
                continue
            format_name = media_format.get("format_name")
            priority = transport_priority.get((protocol_name, format_name))
            if priority is None:
                continue
            codecs = media_format.get("codec")
            if not isinstance(codecs, list):
                continue
            for codec in codecs:
                if not isinstance(codec, dict):
                    continue
                base_url = codec.get("base_url")
                url_info = codec.get("url_info")
                if not isinstance(base_url, str) or not isinstance(url_info, list):
                    continue
                for item in url_info:
                    if not isinstance(item, dict):
                        continue
                    host = item.get("host")
                    extra = item.get("extra") or ""
                    if not isinstance(host, str) or not isinstance(extra, str):
                        continue
                    drm = codec.get("drm") is True
                    codec_priority = 0 if codec.get("codec_name") == "avc" else 1
                    candidates.append(
                        (1 if drm else 0, priority, codec_priority, host + base_url + extra)
                    )
    if not candidates:
        raise RuntimeError("B 站播放地址接口没有找到支持的播放地址")
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    selected = candidates[0]
    return BilibiliStreamInfo(url=selected[3], drm=selected[0] == 1)


def _select_stream_url(playurl: object) -> str:
    return _select_stream_info(playurl).url


def _modern_stream_info(room_id: int, timeout: int) -> BilibiliStreamInfo:
    payload = _fetch_json(
        "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
        f"?room_id={room_id}&protocol=0,1&format=0,1,2&codec=0,1"
        "&qn=10000&platform=web&ptype=8&dolby=5&panorama=1",
        timeout=timeout,
    )
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"B 站播放地址接口失败: code={payload.get('code')}")
    play_info = payload["data"].get("playurl_info")
    playurl = play_info.get("playurl") if isinstance(play_info, dict) else None
    return _select_stream_info(playurl)


def _authenticated_page_stream_info(room_id: int, timeout: int) -> BilibiliStreamInfo:
    html = _fetch_text(f"https://live.bilibili.com/{room_id}", timeout=timeout)
    marker = "window.__NEPTUNE_IS_MY_WAIFU__="
    start = html.find(marker)
    if start < 0:
        raise RuntimeError("B 站登录直播页没有播放器预载数据")
    raw = html[start + len(marker) :].lstrip()
    try:
        bootstrap, _end = json.JSONDecoder().raw_decode(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("B 站登录直播页的播放器预载数据无法解析") from exc
    if not isinstance(bootstrap, dict):
        raise RuntimeError("B 站登录直播页的播放器预载数据格式异常")
    room_init = bootstrap.get("roomInitRes")
    data = room_init.get("data") if isinstance(room_init, dict) else None
    play_info = data.get("playurl_info") if isinstance(data, dict) else None
    playurl = play_info.get("playurl") if isinstance(play_info, dict) else None
    return _select_stream_info(playurl)


def _legacy_stream_info(room_id: int, timeout: int) -> BilibiliStreamInfo:
    """在新版 playurl 为空时回退到 B 站仍可用的公开 durl 接口。"""

    payload = _fetch_json(
        "https://api.live.bilibili.com/room/v1/Room/playUrl"
        f"?cid={room_id}&quality=4&platform=web",
        timeout=timeout,
    )
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"B 站备用播放地址接口失败: code={payload.get('code')}")
    durls = payload["data"].get("durl")
    if not isinstance(durls, list):
        raise RuntimeError("B 站备用播放地址接口没有 durl")
    candidates: list[tuple[int, str]] = []
    for position, item in enumerate(durls):
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        try:
            order = int(item.get("order", position + 1))
        except (TypeError, ValueError):
            order = position + 1
        candidates.append((order, url))
    if not candidates:
        raise RuntimeError("B 站备用播放地址接口没有可用 durl")
    candidates.sort(key=lambda item: item[0])
    return BilibiliStreamInfo(url=candidates[0][1])


def _stream_info(room_id: int, timeout: int) -> BilibiliStreamInfo:
    """优先新版接口，再尝试登录页预载数据，最后降级到公开 durl。"""

    try:
        return _modern_stream_info(room_id, timeout)
    except RuntimeError as modern_error:
        authenticated_error: RuntimeError | None = None
        if os.environ.get("BILIBILI_COOKIE", "").strip():
            try:
                return _authenticated_page_stream_info(room_id, timeout)
            except RuntimeError as exc:
                authenticated_error = exc
        try:
            return _legacy_stream_info(room_id, timeout)
        except RuntimeError as legacy_error:
            authenticated_detail = (
                f"；登录页: {authenticated_error}" if authenticated_error else ""
            )
            raise RuntimeError(
                f"B 站播放地址解析失败；新版: {modern_error}"
                f"{authenticated_detail}；备用: {legacy_error}"
            ) from legacy_error


def _stream_url(room_id: int, timeout: int) -> str:
    return _stream_info(room_id, timeout).url


def fetch_room_info(
    reference: str | int,
    *,
    include_stream: bool = False,
    timeout: int = 20,
) -> BilibiliRoomInfo:
    """解析短号为规范 room_id，并返回当前公开直播状态。"""

    requested_room_id = room_id_from_reference(reference)
    room_payload = _fetch_json(
        "https://api.live.bilibili.com/room/v1/Room/get_info"
        f"?room_id={requested_room_id}",
        timeout=timeout,
    )
    if room_payload.get("code") != 0 or not isinstance(room_payload.get("data"), dict):
        raise RuntimeError(f"B 站房间信息接口失败: code={room_payload.get('code')}")
    room_data = room_payload["data"]
    room_id = int(room_data.get("room_id") or requested_room_id)
    raw_uid = room_data.get("uid")
    creator_uid = str(raw_uid) if raw_uid not in {None, ""} else None
    live_status = int(room_data.get("live_status", 0))
    uname = str(room_data.get("uname") or "未知主播")
    title = str(room_data.get("title") or "")

    try:
        base_payload = _fetch_json(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getRoomBaseInfo"
            f"?req_biz=web_room_compon&room_ids={room_id}",
            timeout=timeout,
        )
        root = base_payload.get("data")
        by_room_ids = root.get("by_room_ids") if isinstance(root, dict) else None
        base_data = by_room_ids.get(str(room_id)) if isinstance(by_room_ids, dict) else None
        if isinstance(base_data, dict):
            uname = str(base_data.get("uname") or uname)
            title = str(base_data.get("title") or title)
            live_status = int(base_data.get("live_status", live_status))
    except (OSError, ValueError, TypeError):
        # get_info 已足够完成状态判断；辅助元数据失败不阻断调度。
        pass

    stream_info = (
        _stream_info(room_id, timeout) if include_stream and live_status == 1 else None
    )
    return BilibiliRoomInfo(
        room_id=room_id,
        requested_room_id=requested_room_id,
        creator_uid=creator_uid,
        uname=uname,
        title=title,
        live_status=live_status,
        stream_url=stream_info.url if stream_info else None,
        stream_drm=stream_info.drm if stream_info else False,
    )
