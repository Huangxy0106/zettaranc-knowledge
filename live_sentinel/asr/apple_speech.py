"""Apple SpeechTranscriber 的本地实时语义转写适配器。

该适配器通过一个 Swift 辅助程序处理已经由上层 VAD 切出的 PCM 语音段。它不
打开麦克风；可用于生产实时语义链路，也可把同一 ``SpeechSegment`` 与云端结果
做影子对照。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import wave

from ..audio.resample import downmix_pcm_s16le, resample_pcm_s16le
from ..models import SpeechSegment, TranscriptSegment


class AppleSpeechCLIStreamingASR:
    def __init__(
        self,
        command: str | Path,
        *,
        locale: str = "zh-CN",
        sample_rate: int = 16_000,
        timeout_sec: float = 90.0,
        default_confidence: float = 0.8,
    ):
        self.command = Path(command).expanduser().resolve()
        if not self.command.is_file():
            raise FileNotFoundError(f"Apple Speech 辅助程序不存在: {self.command}")
        if not os.access(self.command, os.X_OK):
            raise PermissionError(f"Apple Speech 辅助程序不可执行: {self.command}")
        if not locale or sample_rate <= 0 or timeout_sec <= 0:
            raise ValueError("locale、sample_rate 和 timeout_sec 必须有效")
        if not 0.0 <= default_confidence <= 1.0:
            raise ValueError("default_confidence 必须在 [0, 1] 范围内")
        self.locale = locale
        self.sample_rate = sample_rate
        self.timeout_sec = timeout_sec
        self.default_confidence = default_confidence

    def _prepare_pcm(self, speech: SpeechSegment) -> bytes:
        if speech.sample_width != 2:
            raise ValueError("Apple Speech 适配器目前只支持 S16LE")
        pcm = downmix_pcm_s16le(speech.pcm, speech.channels)
        return resample_pcm_s16le(pcm, speech.sample_rate, self.sample_rate)

    def transcribe(self, speech: SpeechSegment) -> TranscriptSegment | None:
        if not speech.pcm:
            return None
        pcm = self._prepare_pcm(speech)
        with tempfile.TemporaryDirectory(prefix="live-sentinel-apple-speech-") as temp:
            audio_path = Path(temp) / "segment.wav"
            with wave.open(str(audio_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(self.sample_rate)
                handle.writeframes(pcm)
            completed = subprocess.run(
                [
                    str(self.command),
                    f"--locale={self.locale}",
                    str(audio_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
        raw = completed.stdout.strip() if completed.returncode == 0 else completed.stderr.strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Apple Speech 辅助程序返回无法解析，returncode={completed.returncode}"
            ) from exc
        if completed.returncode != 0 or payload.get("error"):
            raise RuntimeError(f"Apple Speech 转写失败: {payload.get('error') or 'unknown error'}")
        segments = payload.get("segments")
        if not isinstance(segments, list):
            raise RuntimeError("Apple Speech 返回缺少 segments")
        texts = [
            str(item.get("text") or "").strip()
            for item in segments
            if isinstance(item, dict)
        ]
        text = "".join(item for item in texts if item)
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
        return None
