"""流式 ASR 的适配接口和 OpenAI Realtime 实现。

``SessionManager`` 只要求一个 ``transcribe(SpeechSegment)`` 方法，因此实时服务
可以在 VAD 产出的语音段上提交一个明确的 turn。OpenAI 的 WebSocket 会话在第一
次调用时建立并复用；归档链路不会依赖该连接是否可用。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import json
import threading
from typing import Protocol
import uuid

from ..models import SpeechSegment, TranscriptSegment
from ..audio.resample import downmix_pcm_s16le, resample_pcm_s16le


class StreamingASR(Protocol):
    def transcribe(self, speech: SpeechSegment) -> TranscriptSegment | None:
        ...


class NullStreamingASR:
    """没有接入 ASR 时的安全降级实现。"""

    def transcribe(self, speech: SpeechSegment) -> TranscriptSegment | None:
        return None


class CallableStreamingASR:
    def __init__(self, func: Callable[[SpeechSegment], TranscriptSegment | None]):
        self.func = func

    def transcribe(self, speech: SpeechSegment) -> TranscriptSegment | None:
        return self.func(speech)


class OpenAIRealtimeStreamingASR:
    """使用 OpenAI Realtime transcription WebSocket 的实时转写器。

    当前上层 VAD 以语音段为边界，因此每个 ``transcribe`` 调用会追加音频并
    ``commit`` 一个 turn。服务端仍然返回增量事件，适配器等待对应的 completed
    事件后向系统提交稳定文本；这样不会把 partial 文本误当成正式实时段。

    ``websocket-client`` 是可选依赖，只有实际启用该适配器时才导入。
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-live-transcribe",
        sample_rate: int = 24_000,
        languages: list[str] | tuple[str, ...] = ("zh-cn",),
        keywords: list[str] | tuple[str, ...] = (),
        prompt: str = "",
        delay: str = "low",
        ws_url: str = "wss://api.openai.com/v1/realtime?intent=transcription",
        timeout_sec: float = 20.0,
        default_confidence: float = 0.8,
        socket_factory: Callable[..., object] | None = None,
    ):
        if not api_key:
            raise ValueError("OpenAI Realtime ASR 需要 api_key")
        if sample_rate <= 0 or timeout_sec <= 0:
            raise ValueError("sample_rate 和 timeout_sec 必须为正数")
        if not 0.0 <= default_confidence <= 1.0:
            raise ValueError("default_confidence 必须在 [0, 1] 范围内")
        self.api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.languages = list(languages)
        self.keywords = list(keywords)
        self.prompt = prompt
        self.delay = delay
        self.ws_url = ws_url
        self.timeout_sec = timeout_sec
        self.default_confidence = default_confidence
        self._socket_factory = socket_factory
        self._socket: object | None = None
        self._lock = threading.RLock()

    def _connect(self) -> object:
        if self._socket is not None:
            return self._socket
        factory = self._socket_factory
        if factory is None:
            try:
                import websocket  # type: ignore
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    "OpenAI Realtime ASR 需要 websocket-client；请安装 providers 依赖"
                ) from exc
            factory = websocket.create_connection
        headers = [f"Authorization: Bearer {self.api_key}"]
        socket = factory(self.ws_url, header=headers, timeout=self.timeout_sec)
        self._socket = socket
        self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": self.sample_rate},
                            "transcription": {
                                "model": self.model,
                                **({"languages": self.languages} if self.languages else {}),
                                **({"keywords": self.keywords} if self.keywords else {}),
                                **({"prompt": self.prompt} if self.prompt else {}),
                                **({"delay": self.delay} if self.delay else {}),
                            },
                            "turn_detection": None,
                        }
                    },
                },
            }
        )
        return socket

    def _send(self, payload: dict[str, object]) -> None:
        if self._socket is None:
            raise RuntimeError("OpenAI Realtime ASR WebSocket 尚未连接")
        self._socket.send(json.dumps(payload, ensure_ascii=False))  # type: ignore[attr-defined]

    @staticmethod
    def _message_json(raw: object) -> dict[str, object]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            value = json.loads(raw)
        elif isinstance(raw, dict):
            value = raw
        else:
            raise RuntimeError("Realtime ASR 返回了无法解析的消息")
        if not isinstance(value, dict):
            raise RuntimeError("Realtime ASR 返回消息不是对象")
        return value

    def _receive_completed(self) -> str:
        if self._socket is None:
            raise RuntimeError("OpenAI Realtime ASR WebSocket 尚未连接")
        deltas: list[str] = []
        while True:
            try:
                message = self._socket.recv()  # type: ignore[attr-defined]
            except Exception as exc:
                # Avoid importing a websocket-specific exception at module import time.
                if type(exc).__name__ in {"WebSocketTimeoutException", "TimeoutError"}:
                    raise TimeoutError("OpenAI Realtime ASR 等待结果超时") from exc
                raise
            event = self._message_json(message)
            event_type = str(event.get("type", ""))
            if event_type == "conversation.item.input_audio_transcription.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    deltas.append(delta)
                continue
            if event_type == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript")
                if isinstance(transcript, str):
                    return transcript.strip()
                return "".join(deltas).strip()
            if event_type == "error" or event_type.endswith(".failed"):
                error = event.get("error")
                if isinstance(error, dict):
                    message_text = error.get("message") or error.get("code") or str(error)
                else:
                    message_text = str(error or event)
                raise RuntimeError(f"OpenAI Realtime ASR 错误: {message_text}")

    def _prepare_pcm(self, speech: SpeechSegment) -> bytes:
        pcm = downmix_pcm_s16le(speech.pcm, speech.channels)
        return resample_pcm_s16le(pcm, speech.sample_rate, self.sample_rate)

    def transcribe(self, speech: SpeechSegment) -> TranscriptSegment | None:
        if not speech.pcm:
            return None
        with self._lock:
            self._connect()
            self._send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(self._prepare_pcm(speech)).decode("ascii"),
                }
            )
            self._send({"type": "input_audio_buffer.commit"})
            text = self._receive_completed()
            if not text:
                return None
            return TranscriptSegment(
                start_ms=speech.start_ms,
                end_ms=speech.end_ms,
                text=text,
                confidence=self.default_confidence,
                final=True,
            )

    def close(self) -> None:
        with self._lock:
            if self._socket is None:
                return
            socket = self._socket
            self._socket = None
            try:
                socket.close()  # type: ignore[attr-defined]
            except Exception:
                # Closing a broken realtime socket must not affect P0 archive finalization.
                pass


class DashScopeParaformerRealtimeStreamingASR:
    """使用阿里云百炼 Paraformer 实时 WebSocket 做逐语音段转写。

    上层 VAD 已经把输入切成 ``SpeechSegment``，因此每次调用建立一个
    Paraformer task（同一 WebSocket 可复用多个 task），发送 ``run-task``、
    单声道 PCM 和 ``finish-task``，只把 ``sentence_end=true`` 的稳定结果提交
    给实时缓冲区。连接或任务失败会关闭连接，交给 ``SessionManager`` 记录并
    降级，不影响 P0 音频归档。

    ``websocket-client`` 是可选依赖，只有真正启用此适配器时才导入。Paraformer
    的官方 WebSocket 协议要求 Authorization 在握手阶段提供，任务消息使用
    ``run-task`` / ``finish-task``，服务端事件为 ``task-started``、
    ``result-generated`` 和 ``task-finished``。
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "paraformer-realtime-v2",
        sample_rate: int = 16_000,
        language_hints: list[str] | tuple[str, ...] = ("zh",),
        ws_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        workspace_id: str | None = None,
        timeout_sec: float = 20.0,
        audio_chunk_ms: int = 100,
        semantic_punctuation_enabled: bool = False,
        punctuation_prediction_enabled: bool = True,
        max_sentence_silence_ms: int = 1300,
        heartbeat: bool = False,
        default_confidence: float = 0.8,
        socket_factory: Callable[..., object] | None = None,
    ):
        if not api_key:
            raise ValueError("Paraformer Realtime ASR 需要 api_key")
        if sample_rate <= 0 or timeout_sec <= 0 or audio_chunk_ms <= 0:
            raise ValueError("sample_rate、timeout_sec 和 audio_chunk_ms 必须为正数")
        if max_sentence_silence_ms < 200:
            raise ValueError("max_sentence_silence_ms 不能小于 200ms")
        if not 0.0 <= default_confidence <= 1.0:
            raise ValueError("default_confidence 必须在 [0, 1] 范围内")
        self.api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.language_hints = list(language_hints)
        self.ws_url = ws_url
        self.workspace_id = workspace_id
        self.timeout_sec = timeout_sec
        self.audio_chunk_ms = audio_chunk_ms
        self.semantic_punctuation_enabled = semantic_punctuation_enabled
        self.punctuation_prediction_enabled = punctuation_prediction_enabled
        self.max_sentence_silence_ms = max_sentence_silence_ms
        self.heartbeat = heartbeat
        self.default_confidence = default_confidence
        self._socket_factory = socket_factory
        self._socket: object | None = None
        self._lock = threading.RLock()

    def _resolved_ws_url(self) -> str:
        url = self.ws_url
        if self.workspace_id:
            url = url.replace("{WorkspaceId}", self.workspace_id)
        return url

    def _connect(self) -> object:
        if self._socket is not None:
            return self._socket
        factory = self._socket_factory
        if factory is None:
            try:
                import websocket  # type: ignore
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    "Paraformer Realtime ASR 需要 websocket-client；请安装 providers 依赖"
                ) from exc
            factory = websocket.create_connection
        headers = [f"Authorization: Bearer {self.api_key}"]
        if self.workspace_id:
            headers.append(f"X-DashScope-WorkSpace: {self.workspace_id}")
        self._socket = factory(
            self._resolved_ws_url(),
            header=headers,
            timeout=self.timeout_sec,
        )
        return self._socket

    @staticmethod
    def _message_json(raw: object) -> dict[str, object]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            value = json.loads(raw)
        elif isinstance(raw, dict):
            value = raw
        else:
            raise RuntimeError("Paraformer Realtime ASR 返回了无法解析的消息")
        if not isinstance(value, dict):
            raise RuntimeError("Paraformer Realtime ASR 返回消息不是对象")
        return value

    def _send_json(self, payload: dict[str, object]) -> None:
        if self._socket is None:
            raise RuntimeError("Paraformer Realtime ASR WebSocket 尚未连接")
        self._socket.send(json.dumps(payload, ensure_ascii=False))  # type: ignore[attr-defined]

    def _send_audio(self, pcm: bytes) -> None:
        if self._socket is None:
            raise RuntimeError("Paraformer Realtime ASR WebSocket 尚未连接")
        socket = self._socket
        send_binary = getattr(socket, "send_binary", None)
        if callable(send_binary):
            send_binary(pcm)
            return
        # websocket-client's send(bytes) selects a binary frame automatically;
        # this fallback also keeps the adapter straightforward to fake in tests.
        socket.send(pcm)  # type: ignore[attr-defined]

    def _recv_event(self) -> dict[str, object]:
        if self._socket is None:
            raise RuntimeError("Paraformer Realtime ASR WebSocket 尚未连接")
        try:
            raw = self._socket.recv()  # type: ignore[attr-defined]
        except Exception as exc:
            if type(exc).__name__ in {"WebSocketTimeoutException", "TimeoutError"}:
                raise TimeoutError("Paraformer Realtime ASR 等待结果超时") from exc
            raise
        return self._message_json(raw)

    @staticmethod
    def _event_type(event: dict[str, object]) -> str:
        header = event.get("header")
        return str(header.get("event", "")) if isinstance(header, dict) else ""

    def _send_run_task(self, task_id: str) -> None:
        parameters: dict[str, object] = {
            "format": "pcm",
            "sample_rate": self.sample_rate,
            "semantic_punctuation_enabled": self.semantic_punctuation_enabled,
            "punctuation_prediction_enabled": self.punctuation_prediction_enabled,
            "max_sentence_silence": self.max_sentence_silence_ms,
            "heartbeat": self.heartbeat,
        }
        if self.language_hints:
            parameters["language_hints"] = self.language_hints
        self._send_json(
            {
                "header": {
                    "action": "run-task",
                    "task_id": task_id,
                    "streaming": "duplex",
                },
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": self.model,
                    "input": {},
                    "parameters": parameters,
                },
            }
        )
        while True:
            event = self._recv_event()
            event_type = self._event_type(event)
            if event_type == "task-started":
                return
            if event_type == "task-failed":
                header = event.get("header")
                detail = header if not isinstance(header, dict) else (
                    header.get("error_message") or header.get("error_code")
                )
                raise RuntimeError(f"Paraformer task 启动失败: {detail}")

    def _finish_task_and_collect(self) -> list[dict[str, object]]:
        # task_id is echoed by the server, but the protocol only needs the client
        # generated value. Keep it on the instance for this one locked call.
        task_id = self._task_id
        self._send_json(
            {
                "header": {
                    "action": "finish-task",
                    "task_id": task_id,
                    "streaming": "duplex",
                },
                "payload": {"input": {}},
            }
        )
        final_sentences: list[dict[str, object]] = []
        last_partial: dict[str, object] | None = None
        while True:
            event = self._recv_event()
            event_type = self._event_type(event)
            if event_type == "result-generated":
                payload = event.get("payload")
                output = payload.get("output") if isinstance(payload, dict) else None
                sentence = output.get("sentence") if isinstance(output, dict) else None
                if not isinstance(sentence, dict) or sentence.get("heartbeat"):
                    continue
                if sentence.get("sentence_end") is True:
                    final_sentences.append(sentence)
                else:
                    last_partial = sentence
                continue
            if event_type == "task-failed":
                header = event.get("header")
                if isinstance(header, dict):
                    detail = header.get("error_message") or header.get("error_code")
                else:
                    detail = event
                raise RuntimeError(f"Paraformer task 失败: {detail}")
            if event_type == "task-finished":
                if final_sentences:
                    return final_sentences
                return [last_partial] if last_partial is not None else []

    def _prepare_pcm(self, speech: SpeechSegment) -> bytes:
        pcm = downmix_pcm_s16le(speech.pcm, speech.channels)
        return resample_pcm_s16le(pcm, speech.sample_rate, self.sample_rate)

    def _to_transcript(
        self,
        sentences: list[dict[str, object]],
        speech: SpeechSegment,
    ) -> TranscriptSegment | None:
        usable = [str(item.get("text") or "").strip() for item in sentences]
        usable = [text for text in usable if text]
        if not usable:
            return None
        first = sentences[0]
        last = sentences[-1]
        try:
            relative_start = max(0, int(first.get("begin_time", 0)))
        except (TypeError, ValueError):
            relative_start = 0
        try:
            relative_end = int(last.get("end_time"))
        except (TypeError, ValueError):
            relative_end = speech.duration_ms
        relative_end = max(relative_start, min(speech.duration_ms, relative_end))
        return TranscriptSegment(
            start_ms=speech.start_ms + relative_start,
            end_ms=speech.start_ms + relative_end,
            text="".join(usable),
            confidence=self.default_confidence,
            final=True,
        )

    def transcribe(self, speech: SpeechSegment) -> TranscriptSegment | None:
        if not speech.pcm:
            return None
        with self._lock:
            try:
                self._connect()
                self._task_id = str(uuid.uuid4())
                task_id = self._task_id
                self._send_run_task(task_id)
                pcm = self._prepare_pcm(speech)
                bytes_per_ms = self.sample_rate * 2 / 1000
                chunk_size = max(2, int(bytes_per_ms * self.audio_chunk_ms))
                chunk_size -= chunk_size % 2
                for offset in range(0, len(pcm), chunk_size):
                    self._send_audio(pcm[offset : offset + chunk_size])
                sentences = self._finish_task_and_collect()
                return self._to_transcript(sentences, speech)
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            socket = self._socket
            self._socket = None
            if socket is None:
                return
            try:
                socket.close()  # type: ignore[attr-defined]
            except Exception:
                pass
