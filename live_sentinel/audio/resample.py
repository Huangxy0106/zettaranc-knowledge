"""实时分析所需的立体声到单声道、48kHz 到 16kHz 预处理。"""

from __future__ import annotations

from array import array
import sys

from ..models import AudioFrame


def _as_samples(pcm: bytes) -> array:
    if len(pcm) % 2:
        raise ValueError("S16LE PCM 字节长度必须为偶数")
    samples = array("h")
    samples.frombytes(pcm)
    if samples.itemsize != 2:
        raise RuntimeError("当前平台不支持 16 位 short")
    # array 使用本机字节序；macOS/Linux 通常为 little-endian，显式转换以保证接口稳定。
    if samples.tobytes() != pcm:
        samples.byteswap()
    return samples


def downmix_pcm_s16le(pcm: bytes, channels: int) -> bytes:
    """将任意通道数平均为单声道。"""

    if channels <= 0:
        raise ValueError("channels 必须为正数")
    samples = _as_samples(pcm)
    if len(samples) % channels:
        raise ValueError("PCM 不是完整多通道采样帧")
    if channels == 1:
        return pcm
    mono = array("h")
    for index in range(0, len(samples), channels):
        value = round(sum(samples[index : index + channels]) / channels)
        mono.append(max(-32768, min(32767, value)))
    if sys.byteorder != "little":  # pragma: no cover - CI 通常运行在 little-endian
        mono.byteswap()
    return mono.tobytes()


def resample_pcm_s16le(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """使用线性插值进行单帧块重采样。

    这是 V1 的无依赖实现，适合把采集帧送入实时 ASR；生产环境可以替换为
    高质量的流式重采样器，而不影响上层接口。
    """

    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("采样率必须为正数")
    if source_rate == target_rate:
        return pcm
    source = _as_samples(pcm)
    if not source:
        return b""
    target_count = max(1, round(len(source) * target_rate / source_rate))
    if len(source) == 1:
        result = array("h", [source[0]] * target_count)
        if sys.byteorder != "little":  # pragma: no cover
            result.byteswap()
        return result.tobytes()
    result = array("h")
    scale = (len(source) - 1) / (target_count - 1) if target_count > 1 else 0
    for index in range(target_count):
        position = index * scale
        left = min(len(source) - 1, int(position))
        right = min(len(source) - 1, left + 1)
        fraction = position - left
        value = round(source[left] + (source[right] - source[left]) * fraction)
        result.append(max(-32768, min(32767, value)))
    if sys.byteorder != "little":  # pragma: no cover
        result.byteswap()
    return result.tobytes()


class RealtimeAudioPreprocessor:
    """把归档格式 AudioFrame 转成实时 ASR 的 16kHz mono AudioFrame。"""

    def __init__(self, target_sample_rate: int = 16_000):
        if target_sample_rate <= 0:
            raise ValueError("target_sample_rate 必须为正数")
        self.target_sample_rate = target_sample_rate

    def process(self, frame: AudioFrame) -> AudioFrame:
        if frame.sample_width != 2:
            raise ValueError("V1 预处理器目前只支持 S16LE")
        mono = downmix_pcm_s16le(frame.pcm, frame.channels)
        resampled = resample_pcm_s16le(
            mono, frame.sample_rate, self.target_sample_rate
        )
        return AudioFrame(
            timestamp_ms=frame.timestamp_ms,
            pcm=resampled,
            sample_rate=self.target_sample_rate,
            channels=1,
            sample_width=2,
        )
