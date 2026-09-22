#!/usr/bin/env python3
"""用同一份音频和同一组 VAD 语音段对照 Apple Speech 与 DashScope。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from difflib import SequenceMatcher
import json
from pathlib import Path
import subprocess
import time

from live_sentinel.asr.apple_speech import AppleSpeechCLIStreamingASR
from live_sentinel.asr.realtime import DashScopeParaformerRealtimeStreamingASR
from live_sentinel.asr.vad import EnergyVAD
from live_sentinel.cli import _load_env_file
from live_sentinel.config import load_config
from live_sentinel.integrations import build_realtime_asr
from live_sentinel.models import AudioFrame, SpeechSegment, TranscriptSegment


def _decode_pcm(path: Path, *, sample_rate: int = 16_000) -> bytes:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "pipe:1",
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"ffmpeg 解码失败: {detail}")
    return completed.stdout


def _speech_segments(pcm: bytes, *, sample_rate: int = 16_000) -> list[SpeechSegment]:
    vad = EnergyVAD(rms_threshold=100, silence_ms=600, max_segment_ms=8_000)
    bytes_per_frame = sample_rate * 2 // 10
    segments: list[SpeechSegment] = []
    for offset in range(0, len(pcm), bytes_per_frame):
        chunk = pcm[offset : offset + bytes_per_frame]
        if not chunk:
            continue
        start_ms = offset * 1000 // (sample_rate * 2)
        frame = AudioFrame(
            timestamp_ms=start_ms,
            pcm=chunk,
            sample_rate=sample_rate,
            channels=1,
            sample_width=2,
        )
        segments.extend(vad.process(frame))
    segments.extend(vad.flush())
    return segments


def _call(provider, speech: SpeechSegment) -> tuple[TranscriptSegment | None, float, str | None]:
    started = time.perf_counter()
    try:
        return provider.transcribe(speech), time.perf_counter() - started, None
    except Exception as exc:
        return None, time.perf_counter() - started, f"{type(exc).__name__}: {exc}"


def _text(result: TranscriptSegment | None) -> str:
    return result.text if result is not None else ""


def _markdown(rows: list[dict[str, object]], source: str) -> str:
    successful = [row for row in rows if not row["apple_error"] and not row["dashscope_error"]]
    mean_agreement = (
        sum(float(row["character_agreement"]) for row in successful) / len(successful)
        if successful
        else 0.0
    )
    apple_latency = sum(float(row["apple_latency_sec"]) for row in rows)
    dashscope_latency = sum(float(row["dashscope_latency_sec"]) for row in rows)
    lines = [
        "# Apple Speech / DashScope 实时 ASR 影子对照",
        "",
        f"- 来源：{source}",
        f"- 对照语音段：{len(rows)}",
        f"- 双方成功：{len(successful)}",
        f"- 字符一致度均值（不是准确率）：{mean_agreement:.3f}",
        f"- Apple 总处理时间：{apple_latency:.2f}s",
        f"- DashScope 总等待时间：{dashscope_latency:.2f}s",
        "",
        "> 没有人工逐字稿时，一致度只能表示两套模型彼此接近，不能表示谁更准确。",
        "",
    ]
    for index, row in enumerate(rows, 1):
        lines.extend(
            [
                f"## {index}. {int(row['start_ms']) / 1000:.1f}s–{int(row['end_ms']) / 1000:.1f}s",
                "",
                f"- Apple（{float(row['apple_latency_sec']):.2f}s）：{row['apple_text'] or '[空]'}",
                f"- 阿里（{float(row['dashscope_latency_sec']):.2f}s）：{row['dashscope_text'] or '[空]'}",
                f"- 字符一致度：{float(row['character_agreement']):.3f}",
                f"- Apple 错误：{row['apple_error'] or '[无]'}",
                f"- 阿里错误：{row['dashscope_error'] or '[无]'}",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--apple-command", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-segments", type=int, default=24)
    parser.add_argument("--source", default="local audio")
    args = parser.parse_args()
    if args.max_segments <= 0:
        raise ValueError("--max-segments 必须为正数")

    _load_env_file(args.env_file)
    config = load_config(args.config)
    dashscope, name = build_realtime_asr(config, strict=True)
    if not isinstance(dashscope, DashScopeParaformerRealtimeStreamingASR):
        raise RuntimeError(f"对照要求 DashScope Paraformer，当前为 {name}")
    apple = AppleSpeechCLIStreamingASR(args.apple_command)
    pcm = _decode_pcm(args.audio)
    segments = _speech_segments(pcm)[: args.max_segments]
    if not segments:
        raise RuntimeError("测试音频没有检测到语音段")

    rows: list[dict[str, object]] = []
    try:
        for speech in segments:
            apple_result, apple_latency, apple_error = _call(apple, speech)
            dashscope_result, dashscope_latency, dashscope_error = _call(dashscope, speech)
            apple_text = _text(apple_result)
            dashscope_text = _text(dashscope_result)
            rows.append(
                {
                    "speech": {
                        "start_ms": speech.start_ms,
                        "end_ms": speech.end_ms,
                        "duration_ms": speech.duration_ms,
                    },
                    "start_ms": speech.start_ms,
                    "end_ms": speech.end_ms,
                    "apple": asdict(apple_result) if apple_result else None,
                    "dashscope": asdict(dashscope_result) if dashscope_result else None,
                    "apple_text": apple_text,
                    "dashscope_text": dashscope_text,
                    "apple_latency_sec": round(apple_latency, 4),
                    "dashscope_latency_sec": round(dashscope_latency, 4),
                    "apple_error": apple_error,
                    "dashscope_error": dashscope_error,
                    "character_agreement": round(
                        SequenceMatcher(None, apple_text, dashscope_text).ratio(), 4
                    ),
                }
            )
    finally:
        apple.close()
        dashscope.close()

    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    json_path = output / "comparison.json"
    md_path = output / "comparison.md"
    json_path.write_text(
        json.dumps(
            {"source": args.source, "provider": name, "segments": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path.write_text(_markdown(rows, args.source), encoding="utf-8")
    json_path.chmod(0o600)
    md_path.chmod(0o600)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
