"""文件型离线 ASR 适配器。

OpenAI 的 Transcriptions API 只接受有限大小的文件，而直播 Final Audio 可以持续
数小时。因此适配器先用 ffmpeg 将输入转为 16 kHz 单声道 WAV，并按固定窗口切块，
再逐块上传。若服务返回了带时间戳的 ``segments``，保留其时间戳；否则以块边界
作为保守时间范围，不伪造更细粒度的定位。
"""

from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from collections.abc import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..models import TranscriptSegment


LOGGER = logging.getLogger("live_sentinel.asr.offline")


class OpenAIFileOfflineASR:
    """通过 ``/v1/audio/transcriptions`` 对完整音频做高精度离线转写。"""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-transcribe",
        base_url: str = "https://api.openai.com/v1",
        chunk_seconds: int = 300,
        sample_rate: int = 16_000,
        response_format: str = "json",
        prompt: str = "",
        timeout_sec: float = 120.0,
        ffmpeg_bin: str = "ffmpeg",
        language: str | None = None,
    ):
        if not api_key:
            raise ValueError("OpenAI Offline ASR 需要 api_key")
        if chunk_seconds <= 0 or sample_rate <= 0 or timeout_sec <= 0:
            raise ValueError("chunk_seconds、sample_rate 和 timeout_sec 必须为正数")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.chunk_seconds = chunk_seconds
        self.sample_rate = sample_rate
        self.response_format = response_format
        self.prompt = prompt
        self.timeout_sec = timeout_sec
        self.ffmpeg_bin = ffmpeg_bin
        self.language = language

    def __call__(self, audio_path: str | Path) -> Sequence[TranscriptSegment]:
        return self.transcribe(audio_path)

    def _make_chunks(self, audio_path: Path, directory: Path) -> list[Path]:
        if shutil.which(self.ffmpeg_bin) is None:
            raise RuntimeError("离线 ASR 需要 ffmpeg 进行音频切块")
        output_pattern = directory / "chunk_%06d.wav"
        command = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio_path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(self.sample_rate),
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(self.chunk_seconds),
            "-reset_timestamps",
            "1",
            str(output_pattern),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()[-500:]
            raise RuntimeError(f"ffmpeg 离线切块失败: {detail}") from exc
        chunks = sorted(directory.glob("chunk_*.wav"))
        if not chunks:
            raise RuntimeError("ffmpeg 未生成任何离线 ASR 音频块")
        return chunks

    @staticmethod
    def _duration_ms(path: Path) -> int:
        with wave.open(str(path), "rb") as handle:
            return round(handle.getnframes() * 1000 / handle.getframerate())

    @staticmethod
    def _multipart_body(
        fields: dict[str, str],
        field_name: str,
        file_name: str,
        content_type: str,
        content: bytes,
    ) -> tuple[str, bytes]:
        boundary = f"----live-sentinel-{uuid.uuid4().hex}"
        parts: list[bytes] = []
        for key, value in fields.items():
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{file_name}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        return boundary, b"".join(parts)

    def _upload(self, chunk: Path, prompt: str) -> object:
        fields = {
            "model": self.model,
            "response_format": self.response_format,
        }
        if prompt:
            fields["prompt"] = prompt
        if self.language:
            fields["language"] = self.language
        content_type = mimetypes.guess_type(chunk.name)[0] or "audio/wav"
        boundary, body = self._multipart_body(
            fields,
            "file",
            chunk.name,
            content_type,
            chunk.read_bytes(),
        )
        request = Request(
            f"{self.base_url}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_sec) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"Offline ASR HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Offline ASR 网络请求失败: {exc.reason}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _as_float(value: object, default: float = 0.8) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return max(0.0, min(1.0, number))

    def _parse_response(
        self,
        response: object,
        *,
        offset_ms: int,
        chunk_duration_ms: int,
    ) -> list[TranscriptSegment]:
        if isinstance(response, dict):
            raw_segments = response.get("segments")
            if isinstance(raw_segments, list):
                parsed: list[TranscriptSegment] = []
                for item in raw_segments:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    try:
                        start = max(0.0, float(item.get("start", 0.0)))
                        end = max(start, float(item.get("end", start)))
                    except (TypeError, ValueError):
                        continue
                    parsed.append(
                        TranscriptSegment(
                            start_ms=offset_ms + round(start * 1000),
                            end_ms=offset_ms + round(end * 1000),
                            text=text,
                            confidence=self._as_float(item.get("confidence"), 0.8),
                        )
                    )
                if parsed:
                    return parsed
            text_value = response.get("text")
            text = str(text_value or "").strip()
        else:
            text = str(response or "").strip()
        if not text:
            return []
        # gpt-transcribe returns final text without segment timestamps. Keep the
        # chunk interval explicit rather than inventing word-level positions.
        return [
            TranscriptSegment(
                start_ms=offset_ms,
                end_ms=offset_ms + chunk_duration_ms,
                text=text,
                confidence=0.8,
            )
        ]

    def transcribe(self, audio_path: str | Path) -> list[TranscriptSegment]:
        path = Path(audio_path)
        if not path.exists():
            raise FileNotFoundError(path)
        transcripts: list[TranscriptSegment] = []
        with tempfile.TemporaryDirectory(prefix="live-sentinel-asr-") as directory_name:
            directory = Path(directory_name)
            chunks = self._make_chunks(path, directory)
            offset_ms = 0
            previous_text = ""
            for index, chunk in enumerate(chunks, start=1):
                LOGGER.info("Offline ASR chunk %d/%d started", index, len(chunks))
                duration_ms = self._duration_ms(chunk)
                prompt = self.prompt
                if previous_text:
                    prompt = f"{prompt}\n上一块结尾（仅用于衔接）：{previous_text[-800:]}".strip()
                response = self._upload(chunk, prompt)
                parsed = self._parse_response(
                    response,
                    offset_ms=offset_ms,
                    chunk_duration_ms=duration_ms,
                )
                transcripts.extend(parsed)
                if parsed:
                    previous_text = parsed[-1].text
                offset_ms += duration_ms
                LOGGER.info(
                    "Offline ASR chunk %d/%d completed; segments=%d",
                    index,
                    len(chunks),
                    len(parsed),
                )
        return transcripts


class FunASRLocalOfflineASR(OpenAIFileOfflineASR):
    """调用本机 FunASR OpenAI-compatible HTTP 服务做离线转写。

    FunASR 的服务端默认提供 ``/v1/audio/transcriptions``。该适配器复用文件
    切块和响应时间戳解析逻辑，但不发送云端 API key。通过 ``manage_process``
    可以让它按 Session 管理 ``funasr-server`` 子进程：

    - ``start_mode='session'``：Session 开始时启动，结束时关闭；
    - ``start_mode='lazy'``：第一次离线调用时启动，转写结束后关闭；
    - ``start_mode='external'``：只使用已经运行的服务，不负责启动或关闭。

    服务已经由外部 supervisor 启动时，``start()`` 会复用健康实例，
    ``stop()`` 也不会误杀外部进程。
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:18000/v1",
        health_url: str | None = None,
        model: str = "sensevoice",
        chunk_seconds: int = 300,
        sample_rate: int = 16_000,
        response_format: str = "json",
        prompt: str = "",
        timeout_sec: float = 120.0,
        ffmpeg_bin: str = "ffmpeg",
        language: str | None = "zh",
        manage_process: bool = True,
        server_command: Sequence[str] | None = None,
        start_mode: str = "lazy",
        startup_timeout_sec: float = 90.0,
        shutdown_timeout_sec: float = 10.0,
    ):
        # The parent owns chunking, duration accounting, and conservative timestamp
        # fallback. A non-empty sentinel key is used because the local endpoint does
        # not require authentication and the parent constructor validates the field.
        super().__init__(
            "local-funasr",
            model=model,
            base_url=base_url,
            chunk_seconds=chunk_seconds,
            sample_rate=sample_rate,
            response_format=response_format,
            prompt=prompt,
            timeout_sec=timeout_sec,
            ffmpeg_bin=ffmpeg_bin,
            language=language,
        )
        if start_mode not in {"lazy", "session", "external"}:
            raise ValueError("FunASR start_mode 必须是 lazy、session 或 external")
        if startup_timeout_sec <= 0 or shutdown_timeout_sec <= 0:
            raise ValueError("FunASR 启停超时必须为正数")
        self.start_mode = start_mode
        self.manage_process = manage_process
        self.startup_timeout_sec = startup_timeout_sec
        self.shutdown_timeout_sec = shutdown_timeout_sec
        self.health_url = health_url or self._default_health_url(base_url)
        self.server_command = list(
            server_command
            or (
                "funasr-server",
                "--host",
                "127.0.0.1",
                "--port",
                "18000",
                "--model",
                model,
                "--device",
                "cpu",
            )
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._process_lock = threading.RLock()

    @staticmethod
    def _default_health_url(base_url: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            return base[:-3] + "/health"
        return base + "/health"

    @property
    def process_pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    def is_healthy(self) -> bool:
        request = Request(self.health_url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=min(3.0, self.timeout_sec)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and payload.get("status") in {"ok", "healthy"}

    def start(self) -> None:
        """确保服务健康；已由外部启动时只复用、不接管。"""

        with self._process_lock:
            if self.is_healthy():
                return
            if not self.manage_process or self.start_mode == "external":
                raise RuntimeError(
                    f"FunASR 服务未就绪：{self.health_url}；当前配置不允许由 Session 启动"
                )
            if self._process is not None and self._process.poll() is not None:
                self._process = None
            try:
                self._process = subprocess.Popen(
                    self.server_command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                raise RuntimeError(f"启动 FunASR 服务失败: {exc}") from exc
            deadline = time.monotonic() + self.startup_timeout_sec
            while time.monotonic() < deadline:
                if self.is_healthy():
                    return
                if self._process.poll() is not None:
                    code = self._process.returncode
                    self._process = None
                    raise RuntimeError(f"FunASR 服务提前退出，returncode={code}")
                time.sleep(0.25)
            self.stop()
            raise TimeoutError(f"FunASR 服务在 {self.startup_timeout_sec:.1f}s 内未就绪")

    def stop(self) -> None:
        """仅停止本适配器自己创建的子进程。"""

        with self._process_lock:
            process = self._process
            self._process = None
            if process is None or process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=self.shutdown_timeout_sec)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def _upload(self, chunk: Path, prompt: str) -> object:
        fields = {
            "model": self.model,
            "response_format": self.response_format,
        }
        if self.language:
            fields["language"] = self.language
        content_type = mimetypes.guess_type(chunk.name)[0] or "audio/wav"
        boundary, body = self._multipart_body(
            fields,
            "file",
            chunk.name,
            content_type,
            chunk.read_bytes(),
        )
        request = Request(
            f"{self.base_url}/audio/transcriptions",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_sec) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"FunASR HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"FunASR 网络请求失败: {exc.reason}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", errors="replace")

    def transcribe(self, audio_path: str | Path) -> list[TranscriptSegment]:
        started_here = False
        if self.start_mode != "external":
            if not self.is_healthy():
                self.start()
                started_here = self.process_pid is not None
        elif not self.is_healthy():
            raise RuntimeError(f"FunASR external 服务未就绪：{self.health_url}")
        try:
            return super().transcribe(audio_path)
        finally:
            # Lazy mode owns only the duration of this call. Session mode is closed
            # by OfflineFinalizer/SessionManager after the whole task ends.
            if self.start_mode == "lazy" and started_here:
                self.stop()
