"""飞书新版云文档上传。

实现使用租户应用的 ``tenant_access_token``，先创建文档，再按 Markdown 行转换为
文本/标题/列表 Block 写入。原始文件仍保存在 T5；飞书只承担可检索的文本副本。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..config import FeishuDocsConfig
from ..models import Session


@dataclass(frozen=True)
class FeishuDocument:
    document_id: str
    title: str
    url: str | None = None
    folder_token: str | None = None
    delivery_state: str = "APP_SPACE_CREATED"
    owner_type: str = "application"
    human_access_verified: bool = False
    human_permission: str | None = None


class FeishuCloudDocUploader:
    """创建飞书文档并写入文本内容。"""

    MAX_CHILDREN_PER_REQUEST = 50

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        config: FeishuDocsConfig | None = None,
        folder_token: str | None = None,
        human_member_id: str | None = None,
    ):
        if not app_id or not app_secret:
            raise ValueError("飞书云文档需要 app_id 和 app_secret")
        self.app_id = app_id
        self.app_secret = app_secret
        self.config = config or FeishuDocsConfig()
        if not self.config.base_url:
            raise ValueError("飞书 API base_url 不能为空")
        if self.config.max_blocks_per_request <= 0:
            raise ValueError("max_blocks_per_request 必须为正数")
        if self.config.max_blocks_per_request > self.MAX_CHILDREN_PER_REQUEST:
            raise ValueError(
                "max_blocks_per_request 不能超过飞书接口的 50 个 children 上限"
            )
        if self.config.max_blocks_per_document <= 0:
            raise ValueError("max_blocks_per_document 必须为正数")
        if self.config.timeout_sec <= 0:
            raise ValueError("飞书 timeout_sec 必须为正数")
        if self.config.human_access_required and not human_member_id:
            raise ValueError("飞书人类访问验收需要 human_member_id")
        if self.config.transfer_owner and not self.config.human_access_required:
            raise ValueError("转移所有权要求 human_access_required=true")
        if self.config.human_permission not in {"view", "edit", "full_access"}:
            raise ValueError("飞书 human_permission 必须是 view、edit 或 full_access")
        self.base_url = self.config.base_url.rstrip("/")
        self.folder_token = folder_token or None
        self.human_member_id = human_member_id or None
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        token: str | None = None,
        query: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            def query_value(value: object) -> str:
                if isinstance(value, bool):
                    return "true" if value else "false"
                return str(value)

            url = f"{url}?{urlencode({key: query_value(value) for key, value in query.items()})}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.config.timeout_sec) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(f"飞书云文档 HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"飞书云文档网络请求失败: {exc.reason}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("飞书云文档返回不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("飞书云文档返回不是对象")
        code = payload.get("code")
        if code not in (None, 0, "0"):
            raise RuntimeError(f"飞书云文档返回错误 {code}: {payload.get('msg', '')}")
        return payload

    def _tenant_access_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        payload = self._request_json(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            body={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("飞书认证响应缺少 tenant_access_token")
        try:
            expire = float(payload.get("expire", 7_200))
        except (TypeError, ValueError):
            expire = 7_200.0
        self._token = token
        self._token_expires_at = now + max(60.0, expire - 60.0)
        return token

    @staticmethod
    def _text_block(block_type: int, key: str, content: str) -> dict[str, object]:
        return {
            "block_type": block_type,
            key: {
                "elements": [{"text_run": {"content": content}}],
            },
        }

    @classmethod
    def markdown_blocks(cls, content: str) -> list[dict[str, object]]:
        """把常用 Markdown 行转换为飞书文本 Block。"""

        blocks: list[dict[str, object]] = []
        for raw_line in content.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                continue
            heading = re.match(r"^(#{1,9})\s+(.+)$", line)
            if heading:
                level = len(heading.group(1))
                blocks.append(cls._text_block(2 + level, f"heading{level}", heading.group(2).strip()))
                continue
            if re.match(r"^[-*+]\s+", line):
                blocks.append(cls._text_block(12, "bullet", re.sub(r"^[-*+]\s+", "", line)))
                continue
            ordered = re.match(r"^\d+[.)]\s+(.+)$", line)
            if ordered:
                blocks.append(cls._text_block(13, "ordered", ordered.group(1).strip()))
                continue
            # Very long transcript lines can exceed a single Block's size limit.
            for offset in range(0, len(line), 3_000):
                blocks.append(cls._text_block(2, "text", line[offset : offset + 3_000]))
        return blocks

    def create_folder(self, name: str, *, parent_folder_token: str = "") -> str:
        """在应用可写空间创建文件夹并返回 folder token。"""

        payload = self._request_json(
            "POST",
            "/open-apis/drive/v1/files/create_folder",
            body={"name": name, "folder_token": parent_folder_token},
            token=self._tenant_access_token(),
        )
        data = payload.get("data")
        folder_token = None
        if isinstance(data, dict):
            folder_token = data.get("token") or data.get("folder_token")
        if not isinstance(folder_token, str) or not folder_token:
            raise RuntimeError("飞书创建文件夹响应缺少 folder token")
        return folder_token

    def list_permission_members(
        self,
        token: str,
        *,
        resource_type: str,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            f"/open-apis/drive/v1/permissions/{token}/members",
            token=self._tenant_access_token(),
            query={"type": resource_type, "page_size": 100},
        )
        data = payload.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        if items is None:
            return []
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise RuntimeError("飞书协作者列表响应格式异常")
        return items

    def grant_permission(
        self,
        token: str,
        *,
        resource_type: str,
        member_type: str,
        member_id: str,
        permission: str,
    ) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            f"/open-apis/drive/v1/permissions/{token}/members",
            body={
                "member_type": member_type,
                "member_id": member_id,
                "perm": permission,
            },
            token=self._tenant_access_token(),
            query={"type": resource_type, "need_notification": False},
        )
        data = payload.get("data")
        member = data.get("member") if isinstance(data, dict) else None
        return member if isinstance(member, dict) else {}

    def transfer_resource_owner(
        self,
        token: str,
        *,
        resource_type: str,
        member_type: str,
        member_id: str,
        keep_app_access: bool,
    ) -> None:
        self._request_json(
            "POST",
            f"/open-apis/drive/v1/permissions/{token}/members/transfer_owner",
            body={"member_type": member_type, "member_id": member_id},
            token=self._tenant_access_token(),
            query={
                "type": resource_type,
                "need_notification": False,
                "remove_old_owner": not keep_app_access,
            },
        )

    def ensure_human_access(
        self,
        token: str,
        *,
        resource_type: str,
        transfer_owner: bool,
    ) -> tuple[str, bool, str]:
        """授予、回读人类权限，并可转移所有权。"""

        member_id = self.human_member_id
        if not member_id:
            raise RuntimeError("未配置飞书人类协作者 ID")
        self.grant_permission(
            token,
            resource_type=resource_type,
            member_type=self.config.human_member_type,
            member_id=member_id,
            permission=self.config.human_permission,
        )
        matched: dict[str, Any] | None = None
        # Feishu can acknowledge a permission write before the collaborator is
        # visible to the list API. Retry the readback so a successful grant is
        # not mistaken for a permanent failure.
        for attempt in range(5):
            members = self.list_permission_members(token, resource_type=resource_type)
            matched = next(
                (
                    item
                    for item in members
                    if str(item.get("member_id") or "") == member_id
                    and str(item.get("member_type") or "")
                    == self.config.human_member_type
                ),
                None,
            )
            if matched is not None:
                break
            if attempt < 4:
                time.sleep(0.5 * (attempt + 1))
        actual_permission = str(matched.get("perm") or "") if matched else ""
        if actual_permission != self.config.human_permission:
            raise RuntimeError(
                "飞书人类权限回读不一致: "
                f"expected={self.config.human_permission}, actual={actual_permission or 'missing'}"
            )
        owner_type = "application"
        state = "HUMAN_ACCESS_VERIFIED"
        if transfer_owner:
            self.transfer_resource_owner(
                token,
                resource_type=resource_type,
                member_type=self.config.human_member_type,
                member_id=member_id,
                keep_app_access=self.config.keep_app_after_transfer,
            )
            owner_type = "human"
            state = "OWNERSHIP_TRANSFERRED"
        return state, True, owner_type

    def _finalize_document_access(self, document: FeishuDocument) -> FeishuDocument:
        if not self.config.human_access_required:
            return document
        state, verified, owner_type = self.ensure_human_access(
            document.document_id,
            resource_type="docx",
            transfer_owner=self.config.transfer_owner,
        )
        return replace(
            document,
            delivery_state=state,
            owner_type=owner_type,
            human_access_verified=verified,
            human_permission=self.config.human_permission,
        )

    def create_document(self, title: str, *, folder_token: str | None = None) -> FeishuDocument:
        token = self._tenant_access_token()
        target_folder = folder_token or self.folder_token
        if self.config.require_folder_token and not target_folder:
            raise RuntimeError("飞书交付要求目标文件夹，但未配置 folder token")
        body: dict[str, object] = {"title": title}
        if target_folder:
            body["folder_token"] = target_folder
        payload = self._request_json(
            "POST",
            "/open-apis/docx/v1/documents",
            body=body,
            token=token,
        )
        data = payload.get("data")
        document = data.get("document") if isinstance(data, dict) else None
        document_id = document.get("document_id") if isinstance(document, dict) else None
        if not isinstance(document_id, str) or not document_id:
            raise RuntimeError("飞书创建文档响应缺少 document_id")
        url = None
        if self.config.doc_url_template:
            url = self.config.doc_url_template.format(document_id=document_id)
        return FeishuDocument(
            document_id,
            title,
            url,
            folder_token=target_folder,
            delivery_state=("TARGET_FOLDER_CREATED" if target_folder else "APP_SPACE_CREATED"),
        )

    def append_markdown(self, document: FeishuDocument, content: str) -> FeishuDocument:
        token = self._tenant_access_token()
        blocks = self.markdown_blocks(content)
        for start in range(0, len(blocks), self.config.max_blocks_per_request):
            chunk = blocks[start : start + self.config.max_blocks_per_request]
            self._request_json(
                "POST",
                f"/open-apis/docx/v1/documents/{document.document_id}/blocks/{document.document_id}/children",
                # Omitting index means append to the current end, which is the
                # safest behavior across Feishu API revisions.
                body={"children": chunk},
                token=token,
                query={"document_revision_id": -1},
            )
        return document

    def upload_text(
        self,
        title: str,
        content: str,
        *,
        folder_token: str | None = None,
    ) -> FeishuDocument:
        document = self.create_document(title, folder_token=folder_token)
        document = self.append_markdown(document, content)
        return self._finalize_document_access(document)

    def upload_text_parts(
        self,
        title: str,
        content: str,
        *,
        folder_token: str | None = None,
    ) -> list[FeishuDocument]:
        """按文档 Block 上限拆分长文本，避免单文档超过飞书限制。"""

        blocks = self.markdown_blocks(content)
        if not blocks:
            return [self.upload_text(title, "", folder_token=folder_token)]
        documents: list[FeishuDocument] = []
        total = (len(blocks) + self.config.max_blocks_per_document - 1) // self.config.max_blocks_per_document
        for index, start in enumerate(
            range(0, len(blocks), self.config.max_blocks_per_document), start=1
        ):
            part_blocks = blocks[start : start + self.config.max_blocks_per_document]
            part_title = title if total == 1 else f"{title}｜第 {index}/{total} 部分"
            document = self.create_document(part_title, folder_token=folder_token)
            for request_start in range(0, len(part_blocks), self.config.max_blocks_per_request):
                self._append_blocks(
                    document,
                    part_blocks[request_start : request_start + self.config.max_blocks_per_request],
                )
            documents.append(self._finalize_document_access(document))
        return documents

    def _append_blocks(self, document: FeishuDocument, blocks: list[dict[str, object]]) -> None:
        self._request_json(
            "POST",
            f"/open-apis/docx/v1/documents/{document.document_id}/blocks/{document.document_id}/children",
            body={"children": blocks},
            token=self._tenant_access_token(),
            query={"document_revision_id": -1},
        )

    def upload_session(self, session: Session, final_dir: str | Path) -> list[FeishuDocument]:
        """上传一场 Session 的摘要和全文转写，返回两个文档对象。"""

        directory = Path(final_dir)
        folder_token = self.folder_token
        # The caller normally resolves the environment variable; keeping this
        # method pure avoids ever printing the token.
        summary_parts: list[str] = []
        for name in ("summary.md", "highlights.json", "chapters.json"):
            path = directory / name
            if path.exists():
                summary_parts.append(f"\n## {name}\n\n{path.read_text(encoding='utf-8')}\n")
        transcript_path = directory / "readable.md"
        documents: list[FeishuDocument] = []
        if summary_parts:
            documents.extend(
                self.upload_text_parts(
                    f"直播摘要｜{session.id}",
                    "# 直播摘要\n\n" + "\n".join(summary_parts),
                    folder_token=folder_token,
                )
            )
        if transcript_path.exists():
            documents.extend(
                self.upload_text_parts(
                    f"直播全文转写｜{session.id}",
                    transcript_path.read_text(encoding="utf-8"),
                    folder_token=folder_token,
                )
            )
        return documents
