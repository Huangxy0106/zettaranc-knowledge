"""黑名单主题检测，避免仅凭一个词误杀。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class MatchResult:
    score: float
    matched: tuple[str, ...]


class BlacklistDetector:
    # “不聊星座，继续谈 AI”中的星座不应成为黑名单主题。
    _negation_pattern = re.compile(r"(?:不聊|不谈|不讨论|排除|不是|别聊)[^。！？；,，]{0,8}$")

    def __init__(self, terms: Iterable[str]):
        self.terms = tuple(term.strip() for term in terms if term and term.strip())

    def detect(self, text: str) -> MatchResult:
        matched: list[str] = []
        for term in self.terms:
            for match in re.finditer(re.escape(term), text, flags=re.IGNORECASE):
                prefix = text[max(0, match.start() - 12) : match.start()]
                if self._negation_pattern.search(prefix):
                    continue
                matched.append(term)
                break
        if not self.terms:
            return MatchResult(0.0, ())
        score = min(1.0, len(matched) / min(2, len(self.terms)))
        return MatchResult(score, tuple(matched))
