"""IDLE → CANDIDATE → HOT → COOLING → IDLE 状态机。"""

from __future__ import annotations

from ..config import InterestConfig
from ..models import Highlight, InterestEvent, InterestState


class InterestStateMachine:
    def __init__(self, session_id: str, config: InterestConfig):
        self.session_id = session_id
        self.config = config
        self.state = InterestState.IDLE
        self._hot_streak = 0
        self._leave_streak = 0
        self._cooling_streak = 0
        self._next_highlight = 1
        self._last_notification_ms: int | None = None
        self.active_highlight: Highlight | None = None
        self.highlights: list[Highlight] = []

    def _new_highlight(self, timestamp_ms: int, score: float, topic: str, summary: str) -> Highlight:
        highlight = Highlight(
            id=f"hl_{self._next_highlight:03d}",
            session_id=self.session_id,
            start_ms=timestamp_ms,
            topic=topic,
            score=score,
            summary=summary,
        )
        self._next_highlight += 1
        self.active_highlight = highlight
        self.highlights.append(highlight)
        return highlight

    def _close_highlight(self, end_ms: int) -> str | None:
        if self.active_highlight is None:
            return None
        highlight_id = self.active_highlight.id
        self.active_highlight.close(end_ms)
        self.active_highlight = None
        return highlight_id

    def _notification_allowed(self, timestamp_ms: int) -> bool:
        cooldown = self.config.notification_cooldown_sec * 1000
        return self._last_notification_ms is None or timestamp_ms - self._last_notification_ms >= cooldown

    def update(
        self,
        timestamp_ms: int,
        score: float,
        topic: str = "",
        summary: str = "",
    ) -> InterestEvent | None:
        score = max(0.0, min(1.0, float(score)))
        candidate = self.config.candidate_threshold
        hot = self.config.hot_threshold
        leave = self.config.leave_hot_threshold
        if self.state is InterestState.IDLE:
            if score >= candidate:
                self.state = InterestState.CANDIDATE
                self._hot_streak = 1 if score >= hot else 0
                return InterestEvent("ENTER_CANDIDATE", timestamp_ms, self.state, score)
            return None

        if self.state is InterestState.CANDIDATE:
            if score < candidate:
                self.state = InterestState.IDLE
                self._hot_streak = 0
                return InterestEvent("LEAVE_CANDIDATE", timestamp_ms, self.state, score)
            if score >= hot:
                self._hot_streak += 1
            else:
                self._hot_streak = 0
            if self._hot_streak >= self.config.enter_hot_consecutive_windows:
                self.state = InterestState.HOT
                self._leave_streak = 0
                self._cooling_streak = 0
                highlight = self._new_highlight(timestamp_ms, score, topic, summary)
                notify = self._notification_allowed(timestamp_ms)
                if notify:
                    self._last_notification_ms = timestamp_ms
                return InterestEvent(
                    "ENTER_HOT",
                    timestamp_ms,
                    self.state,
                    score,
                    highlight_id=highlight.id,
                    payload={"notify": notify, "topic": topic, "summary": summary},
                )
            return None

        if self.state is InterestState.HOT:
            if score <= leave:
                self._leave_streak += 1
                if self._leave_streak >= self.config.leave_hot_consecutive_windows:
                    self.state = InterestState.COOLING
                    self._cooling_streak = 1
                    return InterestEvent(
                        "ENTER_COOLING",
                        timestamp_ms,
                        self.state,
                        score,
                        highlight_id=self.active_highlight.id if self.active_highlight else None,
                    )
            else:
                self._leave_streak = 0
            return None

        # COOLING: 给短暂跑题留出缓冲；再次升温可无缝回到 HOT。
        if score >= hot:
            self.state = InterestState.HOT
            self._leave_streak = 0
            self._cooling_streak = 0
            return InterestEvent(
                "REENTER_HOT",
                timestamp_ms,
                self.state,
                score,
                highlight_id=self.active_highlight.id if self.active_highlight else None,
            )
        self._cooling_streak += 1
        if score <= leave and self._cooling_streak >= self.config.leave_hot_consecutive_windows:
            highlight_id = self._close_highlight(timestamp_ms)
            self.state = InterestState.IDLE
            self._hot_streak = 0
            self._leave_streak = 0
            return InterestEvent(
                "LEAVE_HOT",
                timestamp_ms,
                self.state,
                score,
                highlight_id=highlight_id,
            )
        return None

    def close(self, timestamp_ms: int) -> InterestEvent | None:
        """Session 结束时关闭尚未结束的 Highlight。"""

        if self.active_highlight is None:
            return None
        highlight_id = self._close_highlight(timestamp_ms)
        self.state = InterestState.IDLE
        return InterestEvent(
            "LEAVE_HOT",
            timestamp_ms,
            self.state,
            0.0,
            highlight_id=highlight_id,
            payload={"forced": True},
        )
