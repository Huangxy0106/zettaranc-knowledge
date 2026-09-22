#!/usr/bin/env python3
"""使用环境变量创建一篇最小飞书云文档，用于部署连通性验证。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_sentinel.cloud.feishu import FeishuCloudDocUploader
from live_sentinel.config import FeishuDocsConfig


def _load_env_file(path: str | Path = ".live-sentinel.env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def main() -> int:
    _load_env_file()
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    folder_token = os.environ.get("FEISHU_DOCS_FOLDER_TOKEN", "")
    human_member_id = os.environ.get("FEISHU_DOCS_HUMAN_MEMBER_ID", "")
    if not app_id or not app_secret:
        print("需要 FEISHU_APP_ID 和 FEISHU_APP_SECRET", file=sys.stderr)
        return 2
    if not folder_token or not human_member_id:
        print(
            "需要 FEISHU_DOCS_FOLDER_TOKEN 和 FEISHU_DOCS_HUMAN_MEMBER_ID；"
            "不会再向应用私有空间创建测试文档",
            file=sys.stderr,
        )
        return 2
    template = os.environ.get("FEISHU_DOC_URL_TEMPLATE") or None
    config = FeishuDocsConfig(doc_url_template=template)
    uploader = FeishuCloudDocUploader(
        app_id,
        app_secret,
        config=config,
        folder_token=folder_token,
        human_member_id=human_member_id,
    )
    title = datetime.now(timezone.utc).strftime("Live Sentinel 接入验证｜%Y-%m-%d %H:%M UTC")
    document = uploader.upload_text(
        title,
        "# Live Sentinel 接入验证\n\n"
        "- 应用身份鉴权成功\n"
        "- 新版文档创建成功\n"
        "- 文档正文写入成功\n\n"
        "本验证文档不包含直播内容或个人数据。",
    )
    print(f"document_id={document.document_id}")
    if document.url:
        print(f"url={document.url}")
    print(f"delivery_state={document.delivery_state}")
    print(f"owner_type={document.owner_type}")
    print(f"human_access_verified={document.human_access_verified}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
