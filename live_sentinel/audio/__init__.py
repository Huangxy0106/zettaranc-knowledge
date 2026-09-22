"""音频采集、预处理、分流与归档。"""

from .archive import SegmentArchiveWriter
from .finalizer import AudioFinalizer, FinalAudio
from .resample import RealtimeAudioPreprocessor, downmix_pcm_s16le, resample_pcm_s16le
from .segment import CompletedSegment, TimelineGap, validate_timeline
from .source import (
    AudioSource,
    DurationLimitedAudioSource,
    FFmpegAudioSource,
    IterableAudioSource,
    NonSilenceProbeAudioSource,
    QueueAudioSource,
    StartHookAudioSource,
)
from .tee import AudioTee, TeeStats

__all__ = [
    "AudioFinalizer",
    "AudioSource",
    "AudioTee",
    "CompletedSegment",
    "DurationLimitedAudioSource",
    "FinalAudio",
    "FFmpegAudioSource",
    "IterableAudioSource",
    "NonSilenceProbeAudioSource",
    "QueueAudioSource",
    "StartHookAudioSource",
    "RealtimeAudioPreprocessor",
    "SegmentArchiveWriter",
    "TeeStats",
    "TimelineGap",
    "downmix_pcm_s16le",
    "resample_pcm_s16le",
    "validate_timeline",
]
