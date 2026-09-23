"""配置模型与加载器。

V1 运行时可直接使用 ``AppConfig`` 或从 JSON/YAML 文件加载。YAML 读取优先
使用环境中已有的 PyYAML；没有 PyYAML 时给出明确提示，而不是静默使用错误配置。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass
class AudioConfig:
    archive_sample_rate: int = 48_000
    archive_channels: int = 2
    archive_sample_width: int = 2
    archive_codec: str = "flac"
    segment_minutes: int = 120
    merge_after_session: bool = True
    keep_segments_after_finalize_days: int = 1
    working_dir: str = "sessions"


@dataclass
class BilibiliCaptureConfig:
    """B 站房间级采集策略。"""

    # These rooms always use an authenticated browser plus system-audio
    # capture, even while the public stream URL is still available.  This
    # avoids changing capture transports when a live session becomes paid.
    browser_audio_room_ids: list[str] = field(default_factory=list)
    # System playback can target a macOS Multi-Output Device while FFmpeg keeps
    # capturing the virtual device named by BILIBILI_DRM_AUDIO_DEVICE. Empty
    # preserves the original silent behavior by routing to the capture device.
    playback_output_device: str = ""
    status_poll_interval_sec: int = 30
    offline_confirmations: int = 3
    offline_confirmation_interval_sec: int = 5
    continuous_silence_timeout_sec: int = 180
    silence_rms_threshold: float = 100.0


@dataclass
class RealtimeAsrConfig:
    # 关闭后暂停整条实时智能链路：实时转写、实时高价值判断和即时通知。
    # P0 录音、Final Audio 和会后 Offline ASR 不受影响。
    enabled: bool = True
    # Apple Speech and Paraformer both accept the 16 kHz mono output produced by
    # the VAD/preprocessor. OpenAI remains available by setting provider=openai
    # in a project config.
    sample_rate: int = 16_000
    model: str = "paraformer-realtime-v2"
    provider: str = "dashscope"
    # Apple SpeechTranscriber runs as a local Swift helper and reads VAD audio
    # files; it never opens the microphone. These fields are ignored by cloud
    # providers.
    apple_command: str = ""
    apple_locale: str = "zh-CN"
    api_key_env: str = "DASHSCOPE_API_KEY"
    ws_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    languages: list[str] = field(default_factory=lambda: ["zh-cn"])
    # DashScope Paraformer uses ISO-like language hints (for example ``zh``),
    # while the OpenAI adapter uses ``languages`` above. These fields are
    # ignored by the OpenAI provider and keep the two wire protocols explicit.
    language_hints: list[str] = field(default_factory=lambda: ["zh"])
    workspace_id: str | None = None
    audio_chunk_ms: int = 100
    semantic_punctuation_enabled: bool = False
    punctuation_prediction_enabled: bool = True
    max_sentence_silence_ms: int = 1300
    heartbeat: bool = False
    keywords: list[str] = field(default_factory=list)
    prompt: str = "中文直播口播，可能包含人工智能、Agent、投资和科技产业术语。"
    delay: str = "low"
    timeout_sec: float = 20.0


@dataclass
class LocalOfflineAsrConfig:
    """本机 FunASR 服务配置；服务可由 Session 临时管理。"""

    base_url: str = "http://127.0.0.1:18000/v1"
    health_url: str = "http://127.0.0.1:18000/health"
    model: str = "sensevoice"
    language: str | None = "zh"
    manage_process: bool = True
    # ``lazy`` 在离线加工开始时启动；``session`` 在整个任务开始时启动；
    # ``external`` 只复用已存在的服务，不由本进程启动或关闭。
    start_mode: str = "lazy"
    server_command: list[str] = field(
        default_factory=lambda: [
            "funasr-server",
            "--host",
            "127.0.0.1",
            "--port",
            "18000",
            "--model",
            "sensevoice",
            "--device",
            "cpu",
        ]
    )
    startup_timeout_sec: float = 90.0
    shutdown_timeout_sec: float = 10.0


@dataclass
class OfflineAsrConfig:
    model: str = "gpt-transcribe"
    provider: str = "openai"
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str = "https://api.openai.com/v1"
    # OpenAI file transcription has a 25 MB upload limit. Five minutes of
    # 16 kHz mono PCM is comfortably below that limit after conversion.
    chunk_seconds: int = 300
    sample_rate: int = 16_000
    response_format: str = "json"
    prompt: str = "中文直播口播，可能包含人工智能、Agent、投资和科技产业术语。"
    timeout_sec: float = 120.0
    local: LocalOfflineAsrConfig = field(default_factory=LocalOfflineAsrConfig)


@dataclass
class AsrConfig:
    realtime: RealtimeAsrConfig = field(default_factory=RealtimeAsrConfig)
    offline: OfflineAsrConfig = field(default_factory=OfflineAsrConfig)


@dataclass
class AnalysisConfig:
    interval_sec: int = 60
    fast_window_sec: int = 30
    topic_window_sec: int = 180
    llm_window_sec: int = 300
    context_window_sec: int = 900


@dataclass
class LLMConfig:
    """语义 Judge 的 OpenAI-compatible 配置。"""

    # DeepSeek is the default because its API is OpenAI-compatible and its
    # JSON output is sufficient for the small, frequent judge requests.
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com"
    timeout_sec: float = 30.0
    # DeepSeek V4 defaults to thinking mode; a frequent JSON Judge should use
    # the non-thinking path so the output budget is spent on the contract.
    thinking: str | None = "disabled"
    # ``null`` omits the field, which is safest for reasoning-style OpenAI
    # models; DeepSeek can also run deterministically without this parameter.
    temperature: float | None = None
    max_tokens: int = 500


@dataclass
class InterestConfig:
    whitelist: list[str] = field(
        default_factory=lambda: [
            "AI",
            "Agent",
            "大模型",
            "投资",
            "产业",
            "行业",
            "新能源",
            "制造业",
            "科技",
            "芯片",
            "定价权",
            "市场选择",
            "流动性",
            "估值",
            "仓位",
            "策略",
            "资本市场",
            "筹码交换",
            "外资",
            "国家意志",
            "垃圾时间",
            "货币政策",
            "核心资产",
            "国运",
            "交易",
            "预期收益率",
            "市值",
            "大股东",
            "A股",
            "美股",
            "纳斯达克",
            "盈利模式",
        ]
    )
    blacklist: list[str] = field(
        default_factory=lambda: ["星座", "起名", "情感聊天", "日常闲聊"]
    )
    # 低成本规则分只负责决定是否值得调用 LLM；最终候选/重点阈值独立设置。
    # 这样隐含的投资观点不会因为没说出精确关键词而直接写成 semantic=null。
    semantic_trigger_threshold: float = 0.29
    # 即时提醒用于提示用户是否值得上线试听，不要求内容已经形成可执行结论。
    # LLM 语义分主导最终判定；规则分仍负责提供可解释的先验信号。
    candidate_threshold: float = 0.50
    hot_threshold: float = 0.60
    leave_hot_threshold: float = 0.45
    enter_hot_consecutive_windows: int = 2
    leave_hot_consecutive_windows: int = 3
    notification_cooldown_sec: int = 600
    # Keep the high-value logic data-driven: changing these lists in the JSON/YAML
    # config should not require editing Python code.
    structure_markers: list[str] = field(
        default_factory=lambda: [
            "第一",
            "第二",
            "第三",
            "核心",
            "原因",
            "方法",
            "步骤",
            "举个例子",
            "结论",
            "总结",
            "分成",
            "区别",
            "关键",
        ]
    )
    density_markers: list[str] = field(
        default_factory=lambda: [
            "第一",
            "第二",
            "第三",
            "核心",
            "原因",
            "方法",
            "步骤",
            "举个例子",
            "结论",
            "总结",
            "分成",
            "区别",
            "关键",
            "因为",
            "所以",
            "数据",
            "对比",
            "框架",
            "案例",
            "判断",
            "策略",
            "预测",
        ]
    )
    semantic_blend: float = 0.7
    blacklist_multiplier: float = 0.25
    # Formula constants are also data-driven so tuning does not require a code edit.
    tuning: dict[str, float] = field(
        default_factory=lambda: {
            "density_marker_weight": 0.12,
            "density_number_weight": 0.05,
            "long_text_bonus": 0.12,
            "long_text_chars": 100.0,
            "structure_marker_weight": 0.25,
            "novelty_base": 0.35,
            "novelty_unique_divisor": 200.0,
        }
    )
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "whitelist_score": 0.30,
            "blacklist_score": -0.30,
            "information_density": 0.20,
            "topic_continuity": 0.10,
            "structured_speech_score": 0.05,
            "novelty": 0.05,
        }
    )


@dataclass
class FeishuDocsConfig:
    """飞书新版云文档 Open API 配置。"""

    enabled: bool = True
    base_url: str = "https://open.feishu.cn"
    app_id_env: str = "FEISHU_APP_ID"
    app_secret_env: str = "FEISHU_APP_SECRET"
    folder_token_env: str = "FEISHU_DOCS_FOLDER_TOKEN"
    human_member_id_env: str = "FEISHU_DOCS_HUMAN_MEMBER_ID"
    doc_url_template_env: str = "FEISHU_DOC_URL_TEMPLATE"
    # A tenant-specific domain can be supplied when a clickable document URL is
    # desired. Without it the uploader still returns document_id and stores it.
    doc_url_template: str | None = None
    # Production delivery must not silently create documents in the app-only
    # cloud space.  Missing ownership/access configuration disables only the
    # optional Feishu adapter; capture and the verified local archive continue.
    require_folder_token: bool = True
    human_access_required: bool = True
    human_member_type: str = "openid"
    human_permission: str = "full_access"
    transfer_owner: bool = True
    keep_app_after_transfer: bool = True
    max_blocks_per_request: int = 50
    max_blocks_per_document: int = 5_000
    timeout_sec: float = 20.0


@dataclass
class BackupConfig:
    """异地备份配置；当前通过可验证的 bypy CLI 接入百度网盘。"""

    enabled: bool = True
    provider: str = "baidu_bypy"
    command: str = "bypy"
    remote_root: str = "/live-sentinel"
    timeout_sec: float = 3_600.0
    required: bool = False


@dataclass
class StorageConfig:
    """主归档位置与挂载安全策略。"""

    root_dir: str = "sessions"
    require_mounted_path: bool = False
    minimum_free_gib: float = 20.0
    # 设置后，直播采集与会后处理先在内置盘完成；校验复制到 root_dir 后
    # 才删除 staging 副本。跨文件系统不能依赖 rename 的原子性。
    staging_dir: str | None = None
    staging_minimum_free_gib: float = 12.0
    verify_promotion_sha256: bool = True


@dataclass
class WatchlistConfig:
    """长期调度服务配置；desired state 保存在独立 SQLite。"""

    state_db: str = "~/Library/Application Support/LiveSentinel/watchlist.sqlite3"
    # 白天低概率、晚间小概率和周三/周日晚间高概率采用三档轮询。
    # 调度器会在 19:00/24:00 边界主动唤醒，避免低频检查跨过窗口。
    offline_poll_interval_sec: int = 10_800
    evening_poll_interval_sec: int = 3_600
    evening_start_time: str = "19:00"
    evening_end_time: str = "24:00"
    focus_poll_interval_sec: int = 300
    focus_weekdays: list[int] = field(default_factory=lambda: [3, 7])
    focus_start_time: str = "19:00"
    focus_end_time: str = "24:00"
    schedule_timezone: str = "Asia/Shanghai"
    live_poll_interval_sec: int = 300
    error_backoff_base_sec: int = 30
    error_backoff_max_sec: int = 900
    # 连续静音失败达到阈值后暂停较长时间，避免同一场直播反复开页/建会话。
    capture_silence_failure_limit: int = 3
    capture_silence_circuit_break_sec: int = 3_600
    max_concurrent_sessions: int = 1
    max_session_minutes: int = 720
    discovery_timeout_sec: int = 20


@dataclass
class NotificationConfig:
    enabled: bool = True
    cooldown_minutes: int = 10
    provider: str = "feishu_webhook"
    webhook_url_env: str = "FEISHU_WEBHOOK_URL"
    secret_env: str = "FEISHU_WEBHOOK_SECRET"
    # ntfy uses a complete publish endpoint, for example
    # ``https://ntfy.sh/<random-topic>``. Token is optional for public/random
    # topics and is used as a Bearer token for authenticated servers.
    ntfy_url_env: str = "NTFY_URL"
    ntfy_token_env: str = "NTFY_TOKEN"
    ntfy_title: str = "Live Sentinel"
    ntfy_priority: str = "high"
    ntfy_tags: list[str] = field(default_factory=lambda: ["fire"])
    ntfy_click_url: str | None = None
    # Optional separate Feishu group for one-message-per-session summaries.
    summary_enabled: bool = True
    summary_webhook_url_env: str = "FEISHU_SUMMARY_WEBHOOK_URL"
    summary_secret_env: str = "FEISHU_SUMMARY_WEBHOOK_SECRET"
    timeout_sec: float = 10.0


@dataclass
class PostprocessConfig:
    verbatim_transcript: bool = True
    readable_transcript: bool = True
    subtitles: bool = True
    chaptering: bool = True
    highlights: bool = True
    session_summary: bool = True


@dataclass
class AppConfig:
    audio: AudioConfig = field(default_factory=AudioConfig)
    bilibili_capture: BilibiliCaptureConfig = field(
        default_factory=BilibiliCaptureConfig
    )
    asr: AsrConfig = field(default_factory=AsrConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    interest: InterestConfig = field(default_factory=InterestConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    postprocess: PostprocessConfig = field(default_factory=PostprocessConfig)
    feishu_docs: FeishuDocsConfig = field(default_factory=FeishuDocsConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    watchlist: WatchlistConfig = field(default_factory=WatchlistConfig)
    # Kept last to preserve the positional constructor order from V1.
    llm: LLMConfig = field(default_factory=LLMConfig)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AppConfig":
        """将嵌套映射转换为配置对象，并保留未提供字段的默认值。"""

        def build(item_cls: type[Any], key: str) -> Any:
            value = raw.get(key, {})
            if not isinstance(value, Mapping):
                raise TypeError(f"配置段 {key!r} 必须是对象")
            defaults = asdict(item_cls())
            defaults.update(value)
            return item_cls(**defaults)

        asr_raw = raw.get("asr", {})
        if not isinstance(asr_raw, Mapping):
            raise TypeError("配置段 'asr' 必须是对象")
        realtime_raw = asr_raw.get("realtime", {})
        offline_raw = asr_raw.get("offline", {})
        if not isinstance(realtime_raw, Mapping) or not isinstance(offline_raw, Mapping):
            raise TypeError("配置段 'asr.realtime' 和 'asr.offline' 必须是对象")
        realtime = RealtimeAsrConfig(**{**asdict(RealtimeAsrConfig()), **realtime_raw})
        local_raw = offline_raw.get("local", {})
        if not isinstance(local_raw, Mapping):
            raise TypeError("配置段 'asr.offline.local' 必须是对象")
        local = LocalOfflineAsrConfig(**{**asdict(LocalOfflineAsrConfig()), **local_raw})
        offline_defaults = asdict(OfflineAsrConfig())
        offline_defaults.update({key: value for key, value in offline_raw.items() if key != "local"})
        offline_defaults["local"] = local
        offline = OfflineAsrConfig(**offline_defaults)
        asr_defaults = asdict(AsrConfig())
        asr_defaults.update({"realtime": realtime, "offline": offline})

        return cls(
            audio=build(AudioConfig, "audio"),
            bilibili_capture=build(BilibiliCaptureConfig, "bilibili_capture"),
            asr=AsrConfig(**asr_defaults),
            analysis=build(AnalysisConfig, "analysis"),
            llm=build(LLMConfig, "llm"),
            interest=build(InterestConfig, "interest"),
            notification=build(NotificationConfig, "notification"),
            postprocess=build(PostprocessConfig, "postprocess"),
            feishu_docs=build(FeishuDocsConfig, "feishu_docs"),
            backup=build(BackupConfig, "backup"),
            storage=build(StorageConfig, "storage"),
            watchlist=build(WatchlistConfig, "watchlist"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config(path: str | Path) -> AppConfig:
    """从 JSON 或 YAML 文件加载配置。"""

    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        raw = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "读取 YAML 配置需要安装 PyYAML；也可以改用 .json 配置文件"
            ) from exc
        raw = yaml.safe_load(text) or {}
    if not isinstance(raw, Mapping):
        raise TypeError("配置根节点必须是对象")
    return AppConfig.from_mapping(raw)
