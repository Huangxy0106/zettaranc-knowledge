"""根据配置和环境变量组装生产适配器。

该模块不读取或打印密钥内容。默认 ``strict=False``，缺少某个外部凭据时只让
对应能力降级，P0 音频归档仍可运行；部署/CI 可以用 ``strict=True`` 把配置错误
提前变成启动失败。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
import os

from .analysis.llm_judge import (
    DeepSeekLLMJudge,
    LLMJudge,
    NullLLMJudge,
    OpenAILLMJudge,
)
from .asr.apple_speech import AppleSpeechCLIStreamingASR
from .asr.offline import FunASRLocalOfflineASR, OpenAIFileOfflineASR
from .asr.realtime import (
    DashScopeParaformerRealtimeStreamingASR,
    NullStreamingASR,
    OpenAIRealtimeStreamingASR,
    StreamingASR,
)
from .config import AppConfig
from .backup.baidu import BaiduNetdiskBackup
from .cloud.feishu import FeishuCloudDocUploader
from .models import TranscriptSegment
from .notification.notifier import (
    ConsoleNotifier,
    FeishuWebhookNotifier,
    Notifier,
    NtfyNotifier,
)


@dataclass
class IntegrationBundle:
    realtime_asr: StreamingASR
    offline_asr: Callable[[str], Sequence[TranscriptSegment]] | None
    llm_judge: LLMJudge | None
    notifier: Notifier | None
    summary_notifier: FeishuWebhookNotifier | NtfyNotifier | None
    feishu_docs: FeishuCloudDocUploader | None
    backup: BaiduNetdiskBackup | None
    realtime_name: str
    offline_name: str
    llm_name: str
    notifier_name: str
    summary_notifier_name: str
    feishu_docs_name: str
    backup_name: str


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _missing(name: str, strict: bool, capability: str) -> None:
    if strict:
        raise RuntimeError(f"{capability} 已配置但环境变量 {name} 未设置")


def realtime_pipeline_enabled(config: AppConfig) -> bool:
    """是否启用实时转写、实时判断和即时高价值通知整条链路。"""

    settings = config.asr.realtime
    provider = settings.provider.strip().lower()
    return bool(settings.enabled and provider not in {"none", "null", "disabled"})


def build_realtime_asr(config: AppConfig, *, strict: bool = False) -> tuple[StreamingASR, str]:
    settings = config.asr.realtime
    provider = settings.provider.strip().lower()
    if not settings.enabled or provider in {"none", "null", "disabled"}:
        return NullStreamingASR(), "disabled"
    if provider in {"apple", "apple_speech", "speechtranscriber"}:
        if not settings.apple_command.strip():
            message = "Apple Speech 已配置但 asr.realtime.apple_command 未设置"
            if strict:
                raise RuntimeError(message)
            return NullStreamingASR(), "apple/speechtranscriber (missing helper)"
        try:
            adapter = AppleSpeechCLIStreamingASR(
                settings.apple_command,
                locale=settings.apple_locale,
                sample_rate=settings.sample_rate,
                timeout_sec=settings.timeout_sec,
            )
        except (OSError, ValueError):
            if strict:
                raise
            return NullStreamingASR(), "apple/speechtranscriber (invalid helper)"
        return adapter, "apple/speechtranscriber"
    if provider in {"dashscope", "aliyun", "alibaba", "paraformer"}:
        key_env = settings.api_key_env
        if key_env == "OPENAI_API_KEY":
            key_env = "DASHSCOPE_API_KEY"
        api_key = _env(key_env)
        if not api_key:
            _missing(key_env, strict, "Paraformer Realtime ASR")
            return NullStreamingASR(), f"dashscope/{settings.model} (missing key)"
        ws_url = settings.ws_url
        if ws_url.startswith("wss://api.openai.com"):
            ws_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
        return (
            DashScopeParaformerRealtimeStreamingASR(
                api_key,
                model=settings.model,
                sample_rate=settings.sample_rate,
                language_hints=settings.language_hints,
                ws_url=ws_url,
                workspace_id=settings.workspace_id,
                timeout_sec=settings.timeout_sec,
                audio_chunk_ms=settings.audio_chunk_ms,
                semantic_punctuation_enabled=settings.semantic_punctuation_enabled,
                punctuation_prediction_enabled=settings.punctuation_prediction_enabled,
                max_sentence_silence_ms=settings.max_sentence_silence_ms,
                heartbeat=settings.heartbeat,
            ),
            f"dashscope/{settings.model}",
        )
    if provider != "openai":
        raise ValueError(f"不支持的 realtime ASR provider: {settings.provider}")
    api_key = _env(settings.api_key_env)
    if not api_key:
        _missing(settings.api_key_env, strict, "Realtime ASR")
        return NullStreamingASR(), f"openai/{settings.model} (missing key)"
    return (
        OpenAIRealtimeStreamingASR(
            api_key,
            model=settings.model,
            sample_rate=settings.sample_rate,
            languages=settings.languages,
            keywords=settings.keywords,
            prompt=settings.prompt,
            delay=settings.delay,
            ws_url=settings.ws_url,
            timeout_sec=settings.timeout_sec,
        ),
        f"openai/{settings.model}",
    )


def build_offline_asr(
    config: AppConfig, *, strict: bool = False
) -> tuple[Callable[[str], Sequence[TranscriptSegment]] | None, str]:
    settings = config.asr.offline
    provider = settings.provider.strip().lower()
    if provider in {"none", "null", "disabled"}:
        return None, "disabled"
    if provider in {"funasr", "funasr_local", "local_funasr"}:
        local = settings.local
        return (
            FunASRLocalOfflineASR(
                base_url=local.base_url,
                health_url=local.health_url,
                model=local.model,
                chunk_seconds=settings.chunk_seconds,
                sample_rate=settings.sample_rate,
                response_format=settings.response_format,
                prompt=settings.prompt,
                timeout_sec=settings.timeout_sec,
                language=local.language,
                manage_process=local.manage_process,
                server_command=local.server_command,
                start_mode=local.start_mode,
                startup_timeout_sec=local.startup_timeout_sec,
                shutdown_timeout_sec=local.shutdown_timeout_sec,
            ),
            f"funasr_local/{local.model} ({local.start_mode})",
        )
    if provider != "openai":
        raise ValueError(f"不支持的 offline ASR provider: {settings.provider}")
    api_key = _env(settings.api_key_env)
    if not api_key:
        _missing(settings.api_key_env, strict, "Offline ASR")
        return None, f"openai/{settings.model} (missing key)"
    return (
        OpenAIFileOfflineASR(
            api_key,
            model=settings.model,
            base_url=settings.base_url,
            chunk_seconds=settings.chunk_seconds,
            sample_rate=settings.sample_rate,
            response_format=settings.response_format,
            prompt=settings.prompt,
            timeout_sec=settings.timeout_sec,
        ),
        f"openai/{settings.model}",
    )


def build_llm_judge(config: AppConfig, *, strict: bool = False) -> tuple[LLMJudge | None, str]:
    settings = config.llm
    provider = settings.provider.strip().lower()
    if provider in {"none", "null", "disabled"}:
        return None, "disabled"
    if provider not in {"deepseek", "openai", "codex"}:
        raise ValueError(f"不支持的 LLM provider: {settings.provider}")
    key_env = settings.api_key_env
    if provider in {"openai", "codex"} and key_env == "DEEPSEEK_API_KEY":
        key_env = "OPENAI_API_KEY"
    api_key = _env(key_env)
    if not api_key:
        _missing(key_env, strict, "LLM Judge")
        return None, f"{provider}/{settings.model} (missing key)"
    base_url = settings.base_url
    api_model = settings.model
    if provider in {"openai", "codex"}:
        if base_url == "https://api.deepseek.com":
            base_url = "https://api.openai.com/v1"
        judge = OpenAILLMJudge(
            api_key,
            model=api_model,
            base_url=base_url,
            timeout_sec=settings.timeout_sec,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            thinking=None,
        )
        return judge, f"openai/{api_model}"
    judge = DeepSeekLLMJudge(
        api_key,
        model=api_model,
        base_url=base_url,
        timeout_sec=settings.timeout_sec,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        thinking=settings.thinking,
    )
    return judge, f"deepseek/{api_model}"


def build_notifier(config: AppConfig, *, strict: bool = False) -> tuple[Notifier | None, str]:
    settings = config.notification
    if not config.notification.enabled:
        return None, "disabled"
    provider = settings.provider.strip().lower()
    if provider in {"none", "null", "disabled"}:
        return None, "disabled"
    if provider in {"ntfy", "ntfy_push"}:
        publish_url = _env(settings.ntfy_url_env)
        if not publish_url:
            _missing(settings.ntfy_url_env, strict, "ntfy 通知")
            return ConsoleNotifier(), "ntfy (missing URL; console fallback)"
        token = _env(settings.ntfy_token_env)
        return (
            NtfyNotifier(
                publish_url,
                token=token,
                title=settings.ntfy_title,
                priority=settings.ntfy_priority,
                tags=settings.ntfy_tags,
                click_url=settings.ntfy_click_url,
                timeout_sec=settings.timeout_sec,
            ),
            "ntfy" + (" (authenticated)" if token else ""),
        )
    if provider not in {"feishu", "feishu_webhook", "lark"}:
        raise ValueError(f"不支持的 notification provider: {settings.provider}")
    webhook_url = _env(settings.webhook_url_env)
    if not webhook_url:
        _missing(settings.webhook_url_env, strict, "飞书通知")
        return ConsoleNotifier(), "feishu_webhook (missing URL; console fallback)"
    secret = _env(settings.secret_env)
    return (
        FeishuWebhookNotifier(
            webhook_url,
            secret=secret,
            timeout_sec=settings.timeout_sec,
        ),
        "feishu_webhook" + (" (signed)" if secret else ""),
    )


def build_summary_notifier(
    config: AppConfig, *, strict: bool = False
) -> tuple[FeishuWebhookNotifier | None, str]:
    settings = config.notification
    if not settings.summary_enabled:
        return None, "disabled"
    webhook_url = _env(settings.summary_webhook_url_env)
    if not webhook_url:
        if strict:
            raise RuntimeError(
                f"飞书摘要通知已启用但环境变量 {settings.summary_webhook_url_env} 未设置"
            )
        return None, "feishu_summary (missing URL)"
    secret = _env(settings.summary_secret_env)
    return (
        FeishuWebhookNotifier(webhook_url, secret=secret, timeout_sec=settings.timeout_sec),
        "feishu_summary" + (" (signed)" if secret else ""),
    )


def build_feishu_docs(
    config: AppConfig, *, strict: bool = False
) -> tuple[FeishuCloudDocUploader | None, str]:
    settings = config.feishu_docs
    if not settings.enabled:
        return None, "disabled"
    app_id = _env(settings.app_id_env)
    app_secret = _env(settings.app_secret_env)
    if not app_id or not app_secret:
        if strict:
            missing = settings.app_id_env if not app_id else settings.app_secret_env
            raise RuntimeError(f"飞书云文档已启用但环境变量 {missing} 未设置")
        return None, "feishu_docs (missing app credentials)"
    folder_token = _env(settings.folder_token_env)
    if settings.require_folder_token and not folder_token:
        if strict:
            raise RuntimeError(
                f"飞书云文档已启用但环境变量 {settings.folder_token_env} 未设置"
            )
        return None, "feishu_docs (missing target folder)"
    human_member_id = _env(settings.human_member_id_env)
    if settings.human_access_required and not human_member_id:
        if strict:
            raise RuntimeError(
                f"飞书云文档已启用但环境变量 {settings.human_member_id_env} 未设置"
            )
        return None, "feishu_docs (missing human owner)"
    doc_url_template = _env(settings.doc_url_template_env) or settings.doc_url_template
    if doc_url_template != settings.doc_url_template:
        settings = replace(settings, doc_url_template=doc_url_template)
    uploader = FeishuCloudDocUploader(
        app_id,
        app_secret,
        config=settings,
        folder_token=folder_token,
        human_member_id=human_member_id,
    )
    return uploader, "feishu_docs"


def build_backup(config: AppConfig, *, strict: bool = False) -> tuple[BaiduNetdiskBackup | None, str]:
    settings = config.backup
    if not settings.enabled:
        return None, "disabled"
    provider = settings.provider.strip().lower()
    if provider in {"none", "null", "disabled"}:
        return None, "disabled"
    if provider not in {"baidu_bypy", "bypy", "baidu"}:
        raise ValueError(f"不支持的 backup provider: {settings.provider}")
    backup = BaiduNetdiskBackup(
        command=settings.command,
        remote_root=settings.remote_root,
        timeout_sec=settings.timeout_sec,
        required=settings.required,
    )
    if not backup.available():
        if strict or settings.required:
            raise RuntimeError(f"百度网盘备份需要可执行文件: {settings.command}")
        return backup, "baidu_bypy (unavailable; primary archive retained)"
    return backup, "baidu_bypy"


def build_integrations(config: AppConfig, *, strict: bool = False) -> IntegrationBundle:
    realtime_asr, realtime_name = build_realtime_asr(config, strict=strict)
    offline_asr, offline_name = build_offline_asr(config, strict=strict)
    if realtime_pipeline_enabled(config):
        llm_judge, llm_name = build_llm_judge(config, strict=strict)
        notifier, notifier_name = build_notifier(config, strict=strict)
    else:
        llm_judge, llm_name = None, "disabled with realtime pipeline"
        notifier, notifier_name = None, "disabled with realtime pipeline"
    summary_notifier, summary_notifier_name = build_summary_notifier(config, strict=False)
    if summary_notifier is None and config.notification.summary_enabled:
        fallback_notifier = notifier
        fallback_name = notifier_name
        if fallback_notifier is None:
            fallback_notifier, fallback_name = build_notifier(config, strict=strict)
        if fallback_notifier is not None and callable(
            getattr(fallback_notifier, "send_text", None)
        ):
            summary_notifier = fallback_notifier
            summary_notifier_name = f"{fallback_name} (summary fallback)"
        elif strict:
            raise RuntimeError("会话摘要通知已启用，但没有可用的 send_text 通知器")
    feishu_docs, feishu_docs_name = build_feishu_docs(config, strict=strict)
    backup, backup_name = build_backup(config, strict=strict)
    return IntegrationBundle(
        realtime_asr=realtime_asr,
        offline_asr=offline_asr,
        llm_judge=llm_judge,
        notifier=notifier,
        summary_notifier=summary_notifier,
        feishu_docs=feishu_docs,
        backup=backup,
        realtime_name=realtime_name,
        offline_name=offline_name,
        llm_name=llm_name,
        notifier_name=notifier_name,
        summary_notifier_name=summary_notifier_name,
        feishu_docs_name=feishu_docs_name,
        backup_name=backup_name,
    )
