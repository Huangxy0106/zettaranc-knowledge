"""VAD 与实时 ASR 接口。"""

from .apple_speech import AppleSpeechCLIStreamingASR
from .offline import OpenAIFileOfflineASR
from .realtime import (
    CallableStreamingASR,
    DashScopeParaformerRealtimeStreamingASR,
    NullStreamingASR,
    OpenAIRealtimeStreamingASR,
    StreamingASR,
)
from .vad import EnergyVAD

__all__ = [
    "CallableStreamingASR",
    "AppleSpeechCLIStreamingASR",
    "DashScopeParaformerRealtimeStreamingASR",
    "EnergyVAD",
    "NullStreamingASR",
    "OpenAIFileOfflineASR",
    "OpenAIRealtimeStreamingASR",
    "StreamingASR",
]
