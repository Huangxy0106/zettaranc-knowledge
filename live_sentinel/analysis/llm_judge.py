"""LLM Judge 的可替换接口和 OpenAI-compatible API 实现。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Protocol

from ..models import AnalysisFeatures, SemanticResult, TranscriptSegment


class LLMJudge(Protocol):
    def evaluate(
        self, context: Sequence[TranscriptSegment], features: AnalysisFeatures
    ) -> SemanticResult:
        ...


class NullLLMJudge:
    def evaluate(
        self, context: Sequence[TranscriptSegment], features: AnalysisFeatures
    ) -> SemanticResult:
        return SemanticResult(topic=features.topic, score=0.0)


class CallableLLMJudge:
    def __init__(
        self,
        func: Callable[[Sequence[TranscriptSegment], AnalysisFeatures], SemanticResult],
    ):
        self.func = func

    def evaluate(
        self, context: Sequence[TranscriptSegment], features: AnalysisFeatures
    ) -> SemanticResult:
        return self.func(context, features)


class OpenAICompatibleLLMJudge:
    """调用 OpenAI Chat Completions 兼容接口并解析严格 JSON 结果。

    DeepSeek 和 OpenAI 都可以使用此实现。适配器只发送当前 5 分钟窗口与规则
    特征，不把整场直播塞进上下文；HTTP 失败会抛出异常，由 SessionManager 记录
    并降级到规则分数。
    """

    _SYSTEM_PROMPT = """你是直播上线试听提醒判定器，不是聊天助手。
请根据给定的中文直播片段和规则特征，判断是否值得提醒用户上线试听。
投资、产业或行业分析、产业优势、定价权、市场选择、资本市场、宏观政策、
估值、流动性、仓位、交易策略、风险，以及 AI、科技、新能源、制造业和芯片等
相关产业观点，都属于优先关注主题。只要主播正在连续、实质性讨论这些主题，
即使表达口语化、观点尚未收束、没有给出具体标的或操作建议，也应给予 0.75 以上评分；
只是一带而过、纯闲聊、广告或缺乏可辨识观点时才应低分。
is_high_information_density 单独表示内容是否已经达到结构清晰、信息密集的程度，
它可以为 false，但优先关注主题仍可以得到足以触发试听提醒的高 score。
不要补写音频中没有出现的事实；黑名单主题应明确降分。
必须只输出一个合法 JSON 对象，不要 Markdown、不要解释文字。字段必须是：
{"topic": string, "summary": string, "score": number, "is_blacklist_topic": boolean,
"is_high_information_density": boolean}。
score 必须在 0 到 1 之间，summary 不超过 120 个汉字。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        timeout_sec: float = 30.0,
        temperature: float | None = 0.0,
        max_tokens: int = 500,
        thinking: str | None = None,
        system_prompt: str | None = None,
    ):
        if not api_key:
            raise ValueError("LLM Judge 需要 api_key")
        if not base_url or not model:
            raise ValueError("LLM Judge 需要 base_url 和 model")
        if timeout_sec <= 0 or max_tokens <= 0:
            raise ValueError("timeout_sec 和 max_tokens 必须为正数")
        if temperature is not None and not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature 必须在 [0, 2] 范围内")
        if thinking is not None and thinking not in {"enabled", "disabled"}:
            raise ValueError("thinking 必须是 enabled、disabled 或 None")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.system_prompt = system_prompt or self._SYSTEM_PROMPT

    @staticmethod
    def _context_payload(context: Sequence[TranscriptSegment]) -> list[dict[str, object]]:
        return [
            {
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "text": segment.text,
                "confidence": segment.confidence,
            }
            for segment in context
            if segment.text.strip()
        ]

    def _post(self, payload: dict[str, object]) -> object:
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_sec) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(f"LLM Judge HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"LLM Judge 网络请求失败: {exc.reason}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("LLM Judge 返回不是合法 JSON") from exc

    @staticmethod
    def _message_content(response: object) -> str:
        if not isinstance(response, dict):
            raise RuntimeError("LLM Judge 返回结构不是对象")
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("LLM Judge 返回缺少 choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise RuntimeError("LLM Judge choice 结构无效")
        message = first.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("LLM Judge 返回缺少 message")
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict)
            ).strip()
        else:
            text = ""
        if not text:
            raise RuntimeError("LLM Judge 返回空 content")
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        return text

    @staticmethod
    def _bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "是"}
        return bool(value)

    def evaluate(
        self, context: Sequence[TranscriptSegment], features: AnalysisFeatures
    ) -> SemanticResult:
        user_payload = {
            "features": {
                "topic": features.topic,
                "whitelist_score": features.whitelist_score,
                "blacklist_score": features.blacklist_score,
                "information_density": features.information_density,
                "structured_speech_score": features.structured_speech_score,
                "topic_continuity": features.topic_continuity,
                "novelty": features.novelty,
                "speech_ratio": features.speech_ratio,
                "metadata": features.metadata,
            },
            "transcript": self._context_payload(context),
            "output_reminder": "请只返回 JSON 对象，字段必须完整。",
        }
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        # DeepSeek V4 defaults to reasoning mode. The frequent, small Judge
        # request is a classification task, so its DeepSeek subclass disables
        # thinking by default to leave the output budget for the required JSON.
        if self.thinking is not None:
            payload["thinking"] = {"type": self.thinking}
        response = self._post(payload)
        try:
            result = json.loads(self._message_content(response))
        except json.JSONDecodeError as exc:
            raise RuntimeError("LLM Judge content 不是合法 JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("LLM Judge JSON 结果不是对象")
        try:
            score = float(result.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        return SemanticResult(
            topic=str(result.get("topic") or features.topic),
            summary=str(result.get("summary") or "").strip(),
            score=max(0.0, min(1.0, score)),
            is_blacklist_topic=self._bool(result.get("is_blacklist_topic", False)),
            is_high_information_density=self._bool(
                result.get("is_high_information_density", False)
            ),
        )


class DeepSeekLLMJudge(OpenAICompatibleLLMJudge):
    """DeepSeek Judge；模型和接口均可由调用方覆盖。"""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
        timeout_sec: float = 30.0,
        temperature: float | None = None,
        max_tokens: int = 500,
        thinking: str | None = "disabled",
    ):
        super().__init__(
            api_key,
            base_url=base_url,
            model=model,
            timeout_sec=timeout_sec,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking=thinking,
        )


class OpenAILLMJudge(OpenAICompatibleLLMJudge):
    """OpenAI API Judge；也可用 ``model='gpt-5.2-codex'``。"""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-5.6-luna",
        base_url: str = "https://api.openai.com/v1",
        timeout_sec: float = 30.0,
        temperature: float | None = None,
        max_tokens: int = 500,
        thinking: str | None = None,
    ):
        super().__init__(
            api_key,
            base_url=base_url,
            model=model,
            timeout_sec=timeout_sec,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking=thinking,
        )
