"""可解释的兴趣评分。"""

from __future__ import annotations

from ..config import InterestConfig
from ..models import AnalysisFeatures, SemanticResult


class InterestScorer:
    def __init__(self, config: InterestConfig):
        self.config = config

    def score(
        self,
        features: AnalysisFeatures,
        semantic: SemanticResult | None = None,
    ) -> float:
        weights = self.config.weights
        base = sum(
            float(weights.get(name, 0.0)) * float(getattr(features, name))
            for name in (
                "whitelist_score",
                "blacklist_score",
                "information_density",
                "topic_continuity",
                "structured_speech_score",
                "novelty",
            )
        )
        base = max(0.0, min(1.0, base))
        if semantic is None:
            return base
        # LLM 是辅助判断，不取代规则信号；混合比例和黑名单惩罚均可在配置中调整。
        blend = max(0.0, min(1.0, float(self.config.semantic_blend)))
        score = (1.0 - blend) * base + blend * semantic.score
        if semantic.is_blacklist_topic:
            score *= max(0.0, min(1.0, float(self.config.blacklist_multiplier)))
        return max(0.0, min(1.0, score))
