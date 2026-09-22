#!/usr/bin/env python3
"""一次性建立可由人类接管的 Live Sentinel 飞书交付目录。

流程：从一篇已共享给目标用户的应用文档发现唯一 open_id，创建应用目录，
授予并回读 full_access，转移目录所有权但保留应用权限，最后创建一篇无敏感
内容的验收文档并同样转移所有权。脚本不会自动改写凭据文件。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_sentinel.cloud.feishu import FeishuCloudDocUploader
from live_sentinel.config import FeishuDocsConfig


def _env_file(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def _discover_human_member_id(
    uploader: FeishuCloudDocUploader,
    document_id: str,
) -> str:
    members = uploader.list_permission_members(document_id, resource_type="docx")
    candidates = sorted(
        {
            str(member.get("member_id") or "")
            for member in members
            if str(member.get("member_type") or "") == "openid"
            and str(member.get("member_id") or "").startswith("ou_")
        }
    )
    if len(candidates) != 1:
        raise RuntimeError(
            "无法从既有文档唯一确定目标人类账号；"
            f"发现 {len(candidates)} 个 openid 候选，请用 --human-member-id 明确指定"
        )
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="建立飞书人类可接管交付目录")
    parser.add_argument("--env-file", default=".live-sentinel.env")
    parser.add_argument("--existing-doc-id", required=True)
    parser.add_argument("--human-member-id")
    parser.add_argument(
        "--folder-name",
        default="zettaranc 直播档案（Live Sentinel）",
    )
    args = parser.parse_args()

    env = _env_file(args.env_file)
    app_id = env.get("FEISHU_APP_ID", "")
    app_secret = env.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        print("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET", file=sys.stderr)
        return 2

    bootstrap_config = FeishuDocsConfig(
        doc_url_template=env.get("FEISHU_DOC_URL_TEMPLATE") or None,
        require_folder_token=False,
        human_access_required=False,
        transfer_owner=False,
    )
    bootstrap = FeishuCloudDocUploader(app_id, app_secret, config=bootstrap_config)
    human_member_id = args.human_member_id or _discover_human_member_id(
        bootstrap,
        args.existing_doc_id,
    )

    folder_token = bootstrap.create_folder(args.folder_name)
    bootstrap.human_member_id = human_member_id
    bootstrap.config.human_access_required = True
    bootstrap.config.transfer_owner = True
    state, verified, owner_type = bootstrap.ensure_human_access(
        folder_token,
        resource_type="folder",
        transfer_owner=True,
    )
    if not verified or owner_type != "human" or state != "OWNERSHIP_TRANSFERRED":
        raise RuntimeError("目录所有权交付验收失败")

    delivery_config = FeishuDocsConfig(
        doc_url_template=env.get("FEISHU_DOC_URL_TEMPLATE") or None,
    )
    delivery = FeishuCloudDocUploader(
        app_id,
        app_secret,
        config=delivery_config,
        folder_token=folder_token,
        human_member_id=human_member_id,
    )
    title = datetime.now().strftime("Live Sentinel 人类交付验收｜%Y-%m-%d %H:%M")
    document = delivery.upload_text(
        title,
        "# Live Sentinel 飞书交付验收\n\n"
        "- 目标目录已固定\n"
        "- 指定人类账号已获得 full_access\n"
        "- 文档所有权已转移给人类账号\n"
        "- 应用保留后续自动写入权限\n\n"
        "本验收文档不包含直播内容或个人敏感信息。",
    )
    if not document.human_access_verified or document.owner_type != "human":
        raise RuntimeError("验收文档的人类权限或所有权未通过")

    print("RESULT=READY")
    print(f"FEISHU_DOCS_FOLDER_TOKEN={folder_token}")
    print(f"FEISHU_DOCS_HUMAN_MEMBER_ID={human_member_id}")
    print(f"DOCUMENT_ID={document.document_id}")
    print(f"URL={document.url or ''}")
    print(f"DELIVERY_STATE={document.delivery_state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
