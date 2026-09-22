"""可解释的实时内容特征提取器。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from ..models import AnalysisFeatures, TranscriptSegment
from .blacklist import BlacklistDetector
from .whitelist import WhitelistDetector


class RuleBasedContentAnalyzer:
    """V1 规则分析器。

    它的输出是候选筛选信号，不冒充高质量语义理解；当候选分数达到阈值时，
    上层可以再调用注入的 LLM Judge。
    """

    _default_structure_markers = (
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
    )
    _default_density_markers = _default_structure_markers + (
        "因为",
        "所以",
        "数据",
        "对比",
        "框架",
        "案例",
        "判断",
        "策略",
        "预测",
    )

    def __init__(
        self,
        whitelist: Sequence[str],
        blacklist: Sequence[str],
        *,
        structure_markers: Sequence[str] | None = None,
        density_markers: Sequence[str] | None = None,
        tuning: Mapping[str, float] | None = None,
    ):
        self.whitelist_detector = WhitelistDetector(whitelist)
        self.blacklist_detector = BlacklistDetector(blacklist)
        self.structure_markers = tuple(
            structure_markers or self._default_structure_markers
        )
        self.density_markers = tuple(density_markers or self._default_density_markers)
        self.tuning = {
            "density_marker_weight": 0.12,
            "density_number_weight": 0.05,
            "long_text_bonus": 0.12,
            "long_text_chars": 100.0,
            "structure_marker_weight": 0.25,
            "novelty_base": 0.35,
            "novelty_unique_divisor": 200.0,
        }
        if tuning:
            self.tuning.update({key: float(value) for key, value in tuning.items()})
        self._last_topic = ""
        self._last_text = ""

    def _topic(self, text: str, matched: tuple[str, ...]) -> str:
        if matched:
            # 保留配置中的写法，便于提醒文字稳定。
            return matched[0]
        first = re.split(r"[。！？!?\n]", text.strip(), maxsplit=1)[0]
        return first[:32]

    def analyze(
        self,
        segments: Sequence[TranscriptSegment],
        window_duration_ms: int | None = None,
    ) -> AnalysisFeatures:
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
        if not text:
            self._last_topic = ""
            self._last_text = ""
            return AnalysisFeatures()
        white = self.whitelist_detector.detect(text)
        black = self.blacklist_detector.detect(text)
        topic = self._topic(text, white.matched)
        if not self._last_topic:
            continuity = 0.4
        elif topic and (
            topic.casefold() in self._last_topic.casefold()
            or self._last_topic.casefold() in topic.casefold()
            or any(word.casefold() in text.casefold() for word in self._last_topic.split())
        ):
            continuity = 1.0
        else:
            continuity = 0.15
        marker_count = sum(text.count(marker) for marker in self.density_markers)
        structure_count = sum(text.count(marker) for marker in self.structure_markers)
        number_count = len(re.findall(r"(?:\d+(?:\.\d+)?%?|[一二三四五六七八九十]+)", text))
        information_density = min(
            1.0,
            self.tuning["density_marker_weight"] * marker_count
            + self.tuning["density_number_weight"] * number_count,
        )
        if len(text) >= self.tuning["long_text_chars"]:
            information_density = min(
                1.0, information_density + self.tuning["long_text_bonus"]
            )
        structured = min(1.0, self.tuning["structure_marker_weight"] * structure_count)
        if self._last_text and text == self._last_text:
            novelty = 0.0
        else:
            divisor = max(1.0, self.tuning["novelty_unique_divisor"])
            novelty = min(1.0, self.tuning["novelty_base"] + len(set(text)) / divisor)
        if window_duration_ms and window_duration_ms > 0:
            covered_ms = sum(max(0, segment.end_ms - segment.start_ms) for segment in segments)
            speech_ratio = min(1.0, covered_ms / window_duration_ms)
        else:
            speech_ratio = 1.0 if segments else 0.0
        self._last_topic = topic
        self._last_text = text
        return AnalysisFeatures(
            topic=topic,
            whitelist_score=white.score,
            blacklist_score=black.score,
            information_density=information_density,
            structured_speech_score=structured,
            topic_continuity=continuity,
            novelty=novelty,
            speech_ratio=speech_ratio,
            metadata={"whitelist_matches": white.matched, "blacklist_matches": black.matched},
        )
