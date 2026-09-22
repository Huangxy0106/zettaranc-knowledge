"""白名单主题检测。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class MatchResult:
    score: float
    matched: tuple[str, ...]


class WhitelistDetector:
    def __init__(self, terms: Iterable[str]):
        self.terms = tuple(term.strip() for term in terms if term and term.strip())

    def detect(self, text: str) -> MatchResult:
        normalized = text.casefold()
        matched = tuple(
            term for term in self.terms if re.search(re.escape(term.casefold()), normalized)
        )
        if not self.terms:
            return MatchResult(0.0, ())
        # 多个命中只增加置信度，不让关键词堆叠直接把分数推满。
        score = min(1.0, len(matched) / min(3, len(self.terms)))
        return MatchResult(score, matched)
