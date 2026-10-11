"""状态机模块：连续帧确认、点击冷却、验证码停止。

安全规则：
- 连续 N 帧（config: state.consecutive_frames）命中同一目标才确认。
- 同一目标点击后有冷却期（config: state.click_cooldown），避免重复点击。
- 验证码类立即进入 NEED_HUMAN，清空跟踪状态，且绝不产生任何点击动作。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from matcher import Match

logger = logging.getLogger("dsh_screen_ocr.state")


@dataclass
class TargetState:
    """单个目标关键词的跨帧跟踪状态。"""

    key: tuple[str, str, str]  # (category, keyword, text)
    consecutive: int = 0
    center: list[float] = field(default_factory=list)
    last_seen: float = 0.0
    last_click: float = field(default_factory=lambda: float("-inf"))  # 从未点击过


@dataclass
class Decision:
    """一次决策结果。"""

    status: str  # MATCHED | NEED_HUMAN | NO_MATCH | ERROR
    captcha_matches: list[Match] = field(default_factory=list)
    confirmed: list[dict[str, Any]] = field(default_factory=list)  # 已确认（达连续帧数）
    to_click: list[dict[str, Any]] = field(default_factory=list)  # 本轮允许点击
    messages: list[str] = field(default_factory=list)
    video_completed: bool = False  # 是否已检测到视频完成信号（如"重播"）


class StateMachine:
    def __init__(self, config: dict[str, Any]) -> None:
        state_cfg = config.get("state", {}) or {}
        self.consecutive_frames = int(state_cfg.get("consecutive_frames", 3))
        self.click_cooldown = float(state_cfg.get("click_cooldown", 5.0))
        self.center_tolerance = float(state_cfg.get("center_tolerance", 30.0))
        self.reset_timeout = float(state_cfg.get("reset_timeout", 15.0))
        # 需要视频完成信号（如"重播"）才允许点击的关键词
        self.gated_keywords: set[str] = set(state_cfg.get("gated_keywords", []) or [])
        self.video_completed = False
        self._targets: dict[tuple[str, str, str], TargetState] = {}

    def decide(
        self,
        matches: list[Match],
        auto_click: bool,
        now: float | None = None,
    ) -> Decision:
        now = time.time() if now is None else now

        # 1) 验证码类：最高优先级，立即停止，绝不点击
        captcha = [m for m in matches if m.category == "captcha"]
        if captcha:
            self._targets.clear()
            logger.warning("检测到验证码类关键词，进入 NEED_HUMAN，已清空跟踪状态。")
            return Decision(
                status="NEED_HUMAN",
                captcha_matches=captcha,
                messages=["检测到验证码类关键词，需要人工处理，已停止。"],
            )

        # 2) 信号类：如"重播"表示视频播放完成，解锁门控关键词
        for s in matches:
            if s.category == "signal" and s.group == "video_complete":
                if not self.video_completed:
                    logger.info("检测到视频完成信号(%r)，允许点击门控关键词", s.text)
                self.video_completed = True

        actionable = [m for m in matches if m.category in ("next", "continue")]
        if not actionable:
            self._prune(now)
            return Decision(status="NO_MATCH", video_completed=self.video_completed)

        self._prune(now)
        confirmed: list[dict[str, Any]] = []
        to_click: list[dict[str, Any]] = []

        # 2) 更新每个目标关键词的跨帧状态
        for m in actionable:
            key = (m.category, m.keyword, m.text)
            state = self._targets.get(key)
            if state is None:
                state = TargetState(key=key)
                self._targets[key] = state

            same_target = False
            if state.center and state.consecutive > 0:
                dx = state.center[0] - m.center[0]
                dy = state.center[1] - m.center[1]
                if (dx * dx + dy * dy) ** 0.5 <= self.center_tolerance:
                    same_target = True

            if same_target:
                state.consecutive += 1
            else:
                state.consecutive = 1
                state.center = list(m.center)
            state.last_seen = now

            info = {
                "category": m.category,
                "category_label": m.category_label,
                "keyword": m.keyword,
                "text": m.text,
                "confidence": m.confidence,
                "center": [round(m.center[0], 2), round(m.center[1], 2)],
                "consecutive": state.consecutive,
                "required": self.consecutive_frames,
            }

            # 3) 连续命中达阈值才确认；确认后仍受冷却与门控约束
            if state.consecutive >= self.consecutive_frames:
                confirmed.append(info)
                is_gated = m.keyword in self.gated_keywords
                if is_gated and not self.video_completed:
                    logger.debug(
                        "目标 %r 需要视频完成信号(如'重播')，当前未满足，仅确认不点击",
                        m.keyword,
                    )
                elif auto_click and (now - state.last_click) >= self.click_cooldown:
                    state.last_click = now
                    to_click.append(info)
                    logger.info(
                        "目标 [%s] %r 连续命中 %d 帧，触发点击 %s",
                        m.category_label, m.keyword, state.consecutive, info["center"],
                    )
                    if is_gated:
                        # 点击"下一篇"后进入下一节，重置视频完成状态
                        self.video_completed = False
                        logger.info("点击门控目标 %r 后重置视频完成状态", m.keyword)
                elif auto_click:
                    logger.debug(
                        "目标 [%s] %r 已确认但处于冷却期，暂不点击",
                        m.category_label, m.keyword,
                    )
                else:
                    logger.debug(
                        "目标 [%s] %r 已确认，当前为 dry-run，不点击",
                        m.category_label, m.keyword,
                    )

        return Decision(
            status="MATCHED", confirmed=confirmed, to_click=to_click,
            video_completed=self.video_completed,
        )

    def _prune(self, now: float) -> None:
        """清理超时未再出现的目标，重置其连续计数。"""
        stale = [
            k for k, s in self._targets.items()
            if now - s.last_seen > self.reset_timeout
        ]
        for k in stale:
            logger.debug("目标 %s 超过 %.0fs 未出现，重置连续计数", k, self.reset_timeout)
            del self._targets[k]
