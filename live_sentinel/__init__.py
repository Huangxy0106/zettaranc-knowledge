"""B 站直播智能监听与内容归档的 V1 核心库。

包内只依赖 Python 标准库。音频采集、ASR、LLM 和通知服务均通过接口注入，
因此可以先用本地 mock 跑通完整链路，再替换为实际服务适配器。
"""

from .config import AppConfig, LLMConfig, load_config
from .models import (
    AnalysisFeatures,
    AudioFrame,
    Highlight,
    InterestEvent,
    InterestState,
    SemanticResult,
    Session,
    SessionStatus,
    SpeechSegment,
    TranscriptSegment,
)

__all__ = [
    "AnalysisFeatures",
    "AppConfig",
    "AudioFrame",
    "Highlight",
    "InterestEvent",
    "InterestState",
    "LLMConfig",
    "SemanticResult",
    "Session",
    "SessionStatus",
    "SpeechSegment",
    "TranscriptSegment",
    "load_config",
]
