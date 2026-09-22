from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from live_sentinel.backup.baidu import BaiduNetdiskBackup
from live_sentinel.cloud.feishu import FeishuCloudDocUploader
from live_sentinel.config import AppConfig, FeishuDocsConfig
from live_sentinel.integrations import build_integrations
from live_sentinel.notification.notifier import NtfyNotifier


class _DeliveryHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, object]]] = []
    permission_reads_before_visible = 0
    permission_read_count = 0

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                payload = {"raw": body.decode("utf-8")}
        else:
            payload = {}
        self.__class__.requests.append((self.path, payload))
        if self.path.endswith("/tenant_access_token/internal"):
            response = {"code": 0, "tenant_access_token": "test-token", "expire": 7200}
        elif "/permissions/" in self.path and "/transfer_owner?" in self.path:
            response = {"code": 0, "data": {}}
        elif "/permissions/" in self.path and "/members?" in self.path:
            response = {
                "code": 0,
                "data": {
                    "member": {
                        "member_type": "openid",
                        "member_id": "ou-human",
                        "perm": "full_access",
                    }
                },
            }
        elif self.path.endswith("/documents"):
            response = {"code": 0, "data": {"document": {"document_id": "doxc-test"}}}
        elif "/blocks/" in self.path:
            response = {"code": 0, "data": {}}
        else:
            response = {"code": 0, "data": {}}
        raw = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.__class__.requests.append((self.path, {}))
        if "/permissions/" in self.path and "/members?" in self.path:
            self.__class__.permission_read_count += 1
            items = []
            if (
                self.__class__.permission_read_count
                > self.__class__.permission_reads_before_visible
            ):
                items = [
                    {
                        "member_type": "openid",
                        "member_id": "ou-human",
                        "perm": "full_access",
                    }
                ]
            response = {
                "code": 0,
                "data": {"items": items},
            }
        else:
            response = {"code": 0, "data": {}}
        raw = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args: object) -> None:
        return None


class DeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        _DeliveryHandler.requests = []
        _DeliveryHandler.permission_reads_before_visible = 0
        _DeliveryHandler.permission_read_count = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _DeliveryHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_feishu_uploads_markdown_as_document_blocks(self) -> None:
        uploader = FeishuCloudDocUploader(
            "app-id",
            "app-secret",
            config=FeishuDocsConfig(
                base_url=self.base_url,
                doc_url_template="https://tenant.feishu.cn/docx/{document_id}",
                max_blocks_per_request=2,
            ),
            folder_token="fld-human",
            human_member_id="ou-human",
        )
        document = uploader.upload_text("直播摘要", "# 标题\n\n正文\n- 重点\n1. 第二点")
        self.assertEqual(document.document_id, "doxc-test")
        self.assertEqual(document.url, "https://tenant.feishu.cn/docx/doxc-test")
        self.assertEqual(document.delivery_state, "OWNERSHIP_TRANSFERRED")
        self.assertEqual(document.owner_type, "human")
        self.assertTrue(document.human_access_verified)
        paths = [path for path, _payload in _DeliveryHandler.requests]
        self.assertIn("/open-apis/auth/v3/tenant_access_token/internal", paths)
        self.assertIn("/open-apis/docx/v1/documents", paths)
        self.assertGreaterEqual(sum("/blocks/" in path for path in paths), 2)

    @patch("live_sentinel.cloud.feishu.time.sleep")
    def test_feishu_retries_eventually_consistent_permission_readback(
        self,
        sleep: object,
    ) -> None:
        _DeliveryHandler.permission_reads_before_visible = 2
        uploader = FeishuCloudDocUploader(
            "app-id",
            "app-secret",
            config=FeishuDocsConfig(base_url=self.base_url),
            folder_token="fld-human",
            human_member_id="ou-human",
        )

        document = uploader.upload_text("延迟回读", "正文")

        self.assertTrue(document.human_access_verified)
        self.assertEqual(_DeliveryHandler.permission_read_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_feishu_default_batch_never_exceeds_children_limit(self) -> None:
        uploader = FeishuCloudDocUploader(
            "app-id",
            "app-secret",
            config=FeishuDocsConfig(base_url=self.base_url),
            folder_token="fld-human",
            human_member_id="ou-human",
        )
        uploader.upload_text("长文", "\n".join(f"第 {index} 行" for index in range(101)))
        block_payloads = [
            payload for path, payload in _DeliveryHandler.requests if "/blocks/" in path
        ]
        self.assertEqual([len(payload["children"]) for payload in block_payloads], [50, 50, 1])

    def test_feishu_rejects_batch_above_children_limit_before_network(self) -> None:
        with self.assertRaisesRegex(ValueError, "50"):
            FeishuCloudDocUploader(
                "app-id",
                "app-secret",
                config=FeishuDocsConfig(
                    base_url=self.base_url,
                    max_blocks_per_request=51,
                ),
                folder_token="fld-human",
                human_member_id="ou-human",
            )
        self.assertEqual(_DeliveryHandler.requests, [])

    def test_feishu_requires_folder_and_human_owner_before_delivery(self) -> None:
        config = AppConfig()
        with patch.dict(
            "os.environ",
            {
                "FEISHU_APP_ID": "app-id",
                "FEISHU_APP_SECRET": "app-secret",
                "FEISHU_DOCS_FOLDER_TOKEN": "",
                "FEISHU_DOCS_HUMAN_MEMBER_ID": "",
            },
            clear=False,
        ):
            bundle = build_integrations(config)
        self.assertIsNone(bundle.feishu_docs)
        self.assertEqual(bundle.feishu_docs_name, "feishu_docs (missing target folder)")

    def test_feishu_requires_human_owner_when_folder_is_configured(self) -> None:
        config = AppConfig()
        with patch.dict(
            "os.environ",
            {
                "FEISHU_APP_ID": "app-id",
                "FEISHU_APP_SECRET": "app-secret",
                "FEISHU_DOCS_FOLDER_TOKEN": "fld-human",
                "FEISHU_DOCS_HUMAN_MEMBER_ID": "",
            },
            clear=False,
        ):
            bundle = build_integrations(config)
        self.assertIsNone(bundle.feishu_docs)
        self.assertEqual(bundle.feishu_docs_name, "feishu_docs (missing human owner)")

    def test_baidu_backup_missing_bypy_is_nonfatal_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = BaiduNetdiskBackup(command="definitely-not-bypy").backup(directory, "s1")
        self.assertEqual(result.status, "unavailable")

    def test_default_integrations_keep_archive_path_when_optional_credentials_missing(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FEISHU_APP_ID": "",
                "FEISHU_APP_SECRET": "",
                "FEISHU_DOCS_FOLDER_TOKEN": "",
            },
            clear=False,
        ):
            bundle = build_integrations(AppConfig())
        self.assertIsNone(bundle.feishu_docs)
        self.assertIsNotNone(bundle.backup)

    def test_ntfy_can_deliver_plain_text_session_summary(self) -> None:
        notifier = NtfyNotifier(self.base_url, title="Live Sentinel", tags=[])
        notifier.send_text("session completed")
        path, payload = _DeliveryHandler.requests[-1]
        self.assertEqual(path, "/")
        self.assertEqual(payload, {"raw": "session completed"})

    def test_ntfy_is_summary_fallback_when_feishu_webhook_is_missing(self) -> None:
        config = AppConfig.from_mapping(
            {
                "notification": {
                    "provider": "ntfy",
                    "ntfy_url_env": "TEST_NTFY_URL",
                    "summary_enabled": True,
                    "summary_webhook_url_env": "MISSING_SUMMARY_URL",
                }
            }
        )
        with patch.dict(
            "os.environ",
            {"TEST_NTFY_URL": self.base_url, "MISSING_SUMMARY_URL": ""},
            clear=False,
        ):
            bundle = build_integrations(config)
        self.assertIs(bundle.summary_notifier, bundle.notifier)
        self.assertIn("summary fallback", bundle.summary_notifier_name)
