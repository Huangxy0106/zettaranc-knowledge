from __future__ import annotations

import base64
import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from live_sentinel.analysis.llm_judge import OpenAICompatibleLLMJudge
from live_sentinel.asr.apple_speech import AppleSpeechCLIStreamingASR
from live_sentinel.asr.offline import FunASRLocalOfflineASR, OpenAIFileOfflineASR
from live_sentinel.asr.realtime import (
    DashScopeParaformerRealtimeStreamingASR,
    OpenAIRealtimeStreamingASR,
)
from live_sentinel.config import AppConfig
from live_sentinel.integrations import build_integrations, build_realtime_asr
from live_sentinel.models import AnalysisFeatures, Highlight, InterestEvent, InterestState, SpeechSegment, TranscriptSegment
from live_sentinel.notification.notifier import FeishuWebhookNotifier, NtfyNotifier


class _CaptureHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, str], bytes]] = []
    response_payload: dict[str, object] = {"code": 0}

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/health":
            response = json.dumps({"status": "ok"}).encode("utf-8")
        else:
            response = json.dumps(self.__class__.response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.__class__.requests.append((self.path, headers, body))
        response = json.dumps(self.__class__.response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args: object) -> None:
        return None


class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _CaptureHandler.requests = []
        _CaptureHandler.response_payload = {"code": 0}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_openai_compatible_judge_posts_json_and_parses_result(self) -> None:
        _CaptureHandler.response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "topic": "Agent",
                                "summary": "讨论运行时职责边界",
                                "score": 0.91,
                                "is_blacklist_topic": False,
                                "is_high_information_density": True,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        judge = OpenAICompatibleLLMJudge(
            "secret",
            base_url=self.base_url,
            model="mock-model",
        )
        result = judge.evaluate(
            [TranscriptSegment(0, 1000, "Agent Runtime 的职责边界")],
            AnalysisFeatures(topic="Agent", whitelist_score=0.8),
        )
        self.assertEqual(result.topic, "Agent")
        self.assertAlmostEqual(result.score, 0.91)
        self.assertTrue(result.is_high_information_density)
        path, headers, body = _CaptureHandler.requests[0]
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(headers["authorization"], "Bearer secret")
        request = json.loads(body)
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["model"], "mock-model")

        judge = OpenAICompatibleLLMJudge(
            "secret",
            base_url=self.base_url,
            model="mock-model",
            thinking="disabled",
        )
        _CaptureHandler.response_payload = {
            "choices": [{"message": {"content": json.dumps({"score": 0.1})}}]
        }
        judge.evaluate([], AnalysisFeatures())
        second_request = json.loads(_CaptureHandler.requests[-1][2])
        self.assertEqual(second_request["thinking"], {"type": "disabled"})

    def test_feishu_webhook_posts_signed_text(self) -> None:
        notifier = FeishuWebhookNotifier(
            self.base_url + "/hook",
            secret="secret",
        )
        event = InterestEvent("ENTER_HOT", 3_723_000, InterestState.HOT, 0.87)
        highlight = Highlight("hl-1", "session-1", 3_600_000, "Agent", 0.87, "职责边界")
        notifier.send(event, highlight)
        path, _headers, body = _CaptureHandler.requests[0]
        self.assertEqual(path, "/hook")
        payload = json.loads(body)
        self.assertEqual(payload["msg_type"], "text")
        self.assertIn("01:02:03", payload["content"]["text"])
        self.assertIn("timestamp", payload)
        self.assertIn("sign", payload)
        expected = base64.b64encode(
            hmac.new(
                f"{payload['timestamp']}\nsecret".encode("utf-8"),
                b"",
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        self.assertEqual(payload["sign"], expected)

    def test_ntfy_posts_text_with_priority_tags_and_bearer_token(self) -> None:
        notifier = NtfyNotifier(
            self.base_url + "/topic-a",
            token="secret",
            priority="high",
            tags=["fire", "live_sentinel"],
            click_url="https://example.test/session-1",
        )
        event = InterestEvent("ENTER_HOT", 3_723_000, InterestState.HOT, 0.87)
        highlight = Highlight("hl-1", "session-1", 3_600_000, "Agent", 0.87, "职责边界")
        notifier.send(event, highlight)
        path, headers, body = _CaptureHandler.requests[0]
        self.assertEqual(path, "/topic-a")
        self.assertEqual(headers["authorization"], "Bearer secret")
        self.assertEqual(headers["title"], "Live Sentinel")
        self.assertEqual(headers["priority"], "high")
        self.assertEqual(headers["tags"], "fire,live_sentinel")
        self.assertEqual(headers["click"], "https://example.test/session-1")
        text = body.decode("utf-8")
        self.assertIn("主题：Agent", text)
        self.assertIn("时间：01:02:03", text)

    def test_ntfy_provider_uses_configured_environment_variables(self) -> None:
        config = AppConfig.from_mapping(
            {
                "notification": {
                    "provider": "ntfy",
                    "ntfy_url_env": "TEST_NTFY_URL",
                    "ntfy_token_env": "TEST_NTFY_TOKEN",
                    "ntfy_priority": "max",
                    "ntfy_tags": ["warning"],
                }
            }
        )
        with patch.dict(
            "os.environ",
            {
                "TEST_NTFY_URL": self.base_url + "/topic-b",
                "TEST_NTFY_TOKEN": "secret",
            },
            clear=False,
        ):
            bundle = build_integrations(config)
        self.assertIsInstance(bundle.notifier, NtfyNotifier)
        self.assertEqual(bundle.notifier_name, "ntfy (authenticated)")
        assert isinstance(bundle.notifier, NtfyNotifier)
        event = InterestEvent("ENTER_HOT", 0, InterestState.HOT, 0.9)
        bundle.notifier.send(event)
        _path, headers, _body = _CaptureHandler.requests[0]
        self.assertEqual(headers["priority"], "max")
        self.assertEqual(headers["tags"], "warning")

    def test_offline_parser_keeps_provider_timestamps_or_chunk_bounds(self) -> None:
        asr = OpenAIFileOfflineASR("secret", base_url=self.base_url)
        with_timestamps = asr._parse_response(
            {"segments": [{"start": 0.2, "end": 1.4, "text": "你好"}]},
            offset_ms=5_000,
            chunk_duration_ms=3_000,
        )
        self.assertEqual((with_timestamps[0].start_ms, with_timestamps[0].end_ms), (5_200, 6_400))
        without_timestamps = asr._parse_response(
            {"text": "整块文本"}, offset_ms=5_000, chunk_duration_ms=3_000
        )
        self.assertEqual((without_timestamps[0].start_ms, without_timestamps[0].end_ms), (5_000, 8_000))

    def test_realtime_adapter_commits_pcm_and_returns_completed_transcript(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.sent: list[dict[str, object]] = []
                self.messages = [
                    {"type": "session.updated"},
                    {"type": "conversation.item.input_audio_transcription.delta", "delta": "你"},
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "你好",
                    },
                ]

            def send(self, payload: str) -> None:
                self.sent.append(json.loads(payload))

            def recv(self) -> dict[str, object]:
                return self.messages.pop(0)

            def close(self) -> None:
                return None

        socket = FakeSocket()

        def factory(*_args: object, **_kwargs: object) -> FakeSocket:
            return socket

        asr = OpenAIRealtimeStreamingASR(
            "secret",
            sample_rate=16_000,
            socket_factory=factory,
        )
        result = asr.transcribe(SpeechSegment(100, 1_100, b"\x01\x00" * 16_000))
        self.assertEqual(result.text, "你好")
        self.assertEqual([item["type"] for item in socket.sent], ["session.update", "input_audio_buffer.append", "input_audio_buffer.commit"])
        encoded = socket.sent[1]["audio"]
        self.assertEqual(base64.b64decode(encoded), b"\x01\x00" * 16_000)
        asr.close()

    def test_paraformer_adapter_uses_task_protocol_and_stable_sentence(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.json_messages: list[dict[str, object]] = []
                self.audio_chunks: list[bytes] = []
                self.messages = [
                    {"header": {"event": "task-started"}, "payload": {}},
                    {
                        "header": {"event": "result-generated"},
                        "payload": {
                            "output": {
                                "sentence": {
                                    "begin_time": 20,
                                    "end_time": 900,
                                    "text": "你好，世界。",
                                    "sentence_end": True,
                                    "heartbeat": False,
                                }
                            }
                        },
                    },
                    {"header": {"event": "task-finished"}, "payload": {}},
                ]

            def send(self, payload: object) -> None:
                if isinstance(payload, bytes):
                    self.audio_chunks.append(payload)
                else:
                    self.json_messages.append(json.loads(str(payload)))

            def recv(self) -> dict[str, object]:
                return self.messages.pop(0)

            def close(self) -> None:
                return None

        socket = FakeSocket()

        def factory(*_args: object, **_kwargs: object) -> FakeSocket:
            return socket

        asr = DashScopeParaformerRealtimeStreamingASR(
            "secret",
            sample_rate=16_000,
            audio_chunk_ms=100,
            socket_factory=factory,
        )
        result = asr.transcribe(SpeechSegment(100, 1_100, b"\x01\x00" * 16_000))
        assert result is not None
        self.assertEqual(result.text, "你好，世界。")
        self.assertEqual((result.start_ms, result.end_ms), (120, 1000))
        self.assertEqual(socket.json_messages[0]["header"]["action"], "run-task")
        self.assertEqual(socket.json_messages[-1]["header"]["action"], "finish-task")
        self.assertEqual(sum(len(chunk) for chunk in socket.audio_chunks), 32_000)
        asr.close()

    def test_dashscope_realtime_provider_uses_dashscope_key(self) -> None:
        config = AppConfig.from_mapping(
            {
                "asr": {
                    "realtime": {
                        "provider": "dashscope",
                        "model": "paraformer-realtime-v2",
                    }
                }
            }
        )
        with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "secret"}, clear=False):
            bundle = build_integrations(config)
        self.assertIsInstance(bundle.realtime_asr, DashScopeParaformerRealtimeStreamingASR)
        self.assertEqual(bundle.realtime_name, "dashscope/paraformer-realtime-v2")

    def test_apple_realtime_provider_uses_local_helper_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "apple-speech"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            helper.chmod(0o700)
            config = AppConfig.from_mapping(
                {
                    "asr": {
                        "realtime": {
                            "provider": "apple_speech",
                            "model": "speechtranscriber",
                            "apple_command": str(helper),
                            "apple_locale": "zh-CN",
                        }
                    }
                }
            )
            adapter, name = build_realtime_asr(config, strict=True)
        self.assertIsInstance(adapter, AppleSpeechCLIStreamingASR)
        self.assertEqual(name, "apple/speechtranscriber")

    def test_apple_realtime_provider_requires_helper_in_strict_mode(self) -> None:
        config = AppConfig.from_mapping(
            {
                "asr": {
                    "realtime": {
                        "provider": "apple_speech",
                        "apple_command": "/definitely/missing/apple-speech",
                    }
                }
            }
        )
        with self.assertRaises(FileNotFoundError):
            build_realtime_asr(config, strict=True)

    def test_local_funasr_adapter_posts_unauthenticated_multipart(self) -> None:
        _CaptureHandler.response_payload = {"text": "本地转写"}
        asr = FunASRLocalOfflineASR(
            base_url=self.base_url,
            health_url=self.base_url + "/health",
            manage_process=False,
            start_mode="external",
        )
        with tempfile.TemporaryDirectory() as directory:
            chunk = Path(directory) / "chunk.wav"
            chunk.write_bytes(b"RIFF-test")
            response = asr._upload(chunk, "")
        self.assertEqual(response, {"text": "本地转写"})
        path, headers, body = _CaptureHandler.requests[0]
        self.assertEqual(path, "/audio/transcriptions")
        self.assertNotIn("authorization", headers)
        self.assertIn(b'name="model"', body)
        self.assertIn(b"sensevoice", body)

    def test_local_funasr_external_health_is_checked(self) -> None:
        _CaptureHandler.response_payload = {"text": "本地转写"}
        asr = FunASRLocalOfflineASR(
            base_url=self.base_url,
            health_url=self.base_url + "/health",
            manage_process=False,
            start_mode="external",
        )
        self.assertTrue(asr.is_healthy())

    def test_missing_credentials_degrade_only_external_components(self) -> None:
        config = AppConfig()
        bundle = build_integrations(config)
        self.assertIn("missing key", bundle.realtime_name)
        self.assertIn("missing key", bundle.offline_name)
        self.assertIsNone(bundle.llm_judge)
        self.assertIn("missing URL", bundle.notifier_name)

    def test_realtime_pipeline_can_be_disabled_without_realtime_credentials(self) -> None:
        config = AppConfig.from_mapping(
            {
                "asr": {
                    "realtime": {"enabled": False},
                    "offline": {
                        "provider": "funasr_local",
                        "local": {"manage_process": False, "start_mode": "external"},
                    },
                },
                "notification": {
                    "provider": "ntfy",
                    "summary_enabled": False,
                },
                "feishu_docs": {"enabled": False},
                "backup": {"enabled": False},
            }
        )
        with patch.dict("os.environ", {}, clear=True):
            bundle = build_integrations(config, strict=True)
        self.assertEqual(bundle.realtime_name, "disabled")
        self.assertIsNone(bundle.llm_judge)
        self.assertEqual(bundle.llm_name, "disabled with realtime pipeline")
        self.assertIsNone(bundle.notifier)
        self.assertEqual(bundle.notifier_name, "disabled with realtime pipeline")
        self.assertIsInstance(bundle.offline_asr, FunASRLocalOfflineASR)

    def test_funasr_local_provider_is_buildable_without_cloud_key(self) -> None:
        config = AppConfig.from_mapping(
            {
                "asr": {
                    "offline": {
                        "provider": "funasr_local",
                        "local": {
                            "base_url": "http://127.0.0.1:18000/v1",
                            "health_url": "http://127.0.0.1:18000/health",
                            "start_mode": "session",
                        },
                    }
                }
            }
        )
        bundle = build_integrations(config)
        self.assertIsInstance(bundle.offline_asr, FunASRLocalOfflineASR)
        self.assertEqual(bundle.offline_name, "funasr_local/sensevoice (session)")
