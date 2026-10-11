"""关键词匹配模块。

优先级固定：验证码类 > 下一节类 > 继续播放类。
匹配策略：精确匹配优先，其次包含匹配（均可通过 config 调整）。
易误匹配词（如"播放"）通过 ambiguous_words 单独开关与阈值控制。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ocr import OCRResult

logger = logging.getLogger("dsh_screen_ocr.matcher")

# 类别优先级（索引越小越优先），顺序不可改变
CATEGORY_ORDER = ["captcha", "next", "continue"]
CATEGORY_LABEL = {
    "captcha": "验证码类",
    "next": "下一节类",
    "continue": "继续播放类",
    "signal": "信号类",
}
CATEGORY_PRIORITY = {name: idx for idx, name in enumerate(CATEGORY_ORDER)}
SIGNAL_PRIORITY = 99  # 信号类不参与点击优先级，排在最末


@dataclass
class Match:
    category: str
    category_label: str
    keyword: str
    text: str
    confidence: float
    bbox: list[list[float]]
    center: list[float]
    match_type: str  # exact | contains
    priority: int
    group: str = ""  # 信号组名（如 video_complete），普通类别为空

    def to_dict(self) -> dict[str, Any]:
        d = {
            "category": self.category,
            "category_label": self.category_label,
            "keyword": self.keyword,
            "text": self.text,
            "confidence": round(float(self.confidence), 4),
            "match_type": self.match_type,
            "bbox": [[round(float(x), 2), round(float(y), 2)] for x, y in self.bbox],
            "center": [round(float(self.center[0]), 2), round(float(self.center[1]), 2)],
        }
        if self.group:
            d["group"] = self.group
        return d


class Matcher:
    def __init__(self, config: dict[str, Any]) -> None:
        self.keywords_cfg = config.get("keywords", {}) or {}
        match_cfg = config.get("match", {}) or {}
        self.exact_first = bool(match_cfg.get("exact_first", True))
        self.enable_contains = bool(match_cfg.get("enable_contains", True))
        # contains 匹配仅对长度 <= max_text_length 的文本生效，避免正文长句误匹配
        self.max_text_length = int(match_cfg.get("max_text_length", 10))

    def match(self, ocr_results: list[OCRResult]) -> list[Match]:
        """对 OCR 结果做关键词匹配，返回按优先级排序的匹配列表。"""
        matches: list[Match] = []
        seen: set[tuple[str, str, str, str]] = set()

        for category in CATEGORY_ORDER:
            cat_cfg = self.keywords_cfg.get(category, {}) or {}
            if not cat_cfg.get("enabled", True):
                continue

            words = list(cat_cfg.get("words", []))
            ambiguous = cat_cfg.get("ambiguous_words", {}) or {}
            amb_words = list(ambiguous.get("words", [])) if ambiguous.get("enabled", False) else []
            amb_min_conf = float(ambiguous.get("min_confidence", 0.9))

            for r in ocr_results:
                for kw in words:
                    self._add_match(category, kw, r, matches, seen)
                for kw in amb_words:
                    # 易误匹配词要求更高的置信度
                    if r.confidence >= amb_min_conf:
                        self._add_match(category, kw, r, matches, seen)

        # 信号类（不作为点击目标，只作为状态信号，如"重播"）
        signal_cfg = self.keywords_cfg.get("signal", {}) or {}
        for group, grp_cfg in signal_cfg.items():
            if not isinstance(grp_cfg, dict) or not grp_cfg.get("enabled", True):
                continue
            for r in ocr_results:
                for kw in grp_cfg.get("words", []):
                    self._add_signal_match(group, kw, r, matches, seen)

        # 排序：类别优先级 → 精确/包含 → 置信度降序
        matches.sort(key=self._sort_key)
        return matches

    def _sort_key(self, m: Match) -> tuple[int, int, float]:
        type_rank = 0 if m.match_type == "exact" else 1
        if not self.exact_first:
            type_rank = 1 - type_rank
        return (m.priority, type_rank, -m.confidence)

    def _add_match(
        self,
        category: str,
        keyword: str,
        r: OCRResult,
        matches: list[Match],
        seen: set[tuple[str, str, str, str]],
    ) -> None:
        text = r.text
        if keyword not in text:
            return
        match_type = "exact" if text == keyword else "contains"
        if match_type == "contains":
            if not self.enable_contains:
                return
            if len(text) > self.max_text_length:
                # 长文本（如正文句子）不做 contains 匹配，避免误报
                logger.debug(
                    "忽略超长文本(%d字 > %d)的包含匹配: %r",
                    len(text), self.max_text_length, text,
                )
                return
        key = (category, "", keyword, text)
        if key in seen:
            return
        seen.add(key)
        matches.append(
            Match(
                category=category,
                category_label=CATEGORY_LABEL[category],
                keyword=keyword,
                text=text,
                confidence=r.confidence,
                bbox=r.bbox,
                center=list(r.center),
                match_type=match_type,
                priority=CATEGORY_PRIORITY[category],
            )
        )
        logger.debug(
            "命中 [%s] 关键词=%r 文本=%r 方式=%s 置信度=%.3f",
            CATEGORY_LABEL[category], keyword, text, match_type, r.confidence,
        )

    def _add_signal_match(
        self,
        group: str,
        keyword: str,
        r: OCRResult,
        matches: list[Match],
        seen: set[tuple[str, str, str, str]],
    ) -> None:
        text = r.text
        if keyword not in text:
            return
        match_type = "exact" if text == keyword else "contains"
        if match_type == "contains":
            if not self.enable_contains:
                return
            if len(text) > self.max_text_length:
                return
        key = ("signal", group, keyword, text)
        if key in seen:
            return
        seen.add(key)
        matches.append(
            Match(
                category="signal",
                category_label=CATEGORY_LABEL["signal"],
                keyword=keyword,
                text=text,
                confidence=r.confidence,
                bbox=r.bbox,
                center=list(r.center),
                match_type=match_type,
                priority=SIGNAL_PRIORITY,
                group=group,
            )
        )
        logger.debug("信号命中 [%s] 关键词=%r 文本=%r 方式=%s", group, keyword, text, match_type)


if __name__ == "__main__":
    # 无设备自测：构造假 OCR 结果，验证匹配逻辑与优先级。
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    cfg = {
        "keywords": {
            "captcha": {"enabled": True, "words": ["验证码", "请完成验证"]},
            "next": {"enabled": True, "words": ["下一节", "下一篇"]},
            "continue": {
                "enabled": True,
                "words": ["继续播放", "继续"],
                "ambiguous_words": {"enabled": False, "words": ["播放"], "min_confidence": 0.9},
            },
            "signal": {"video_complete": {"enabled": True, "words": ["重播"]}},
        },
        "match": {"exact_first": True, "enable_contains": True},
    }
    matcher = Matcher(cfg)
    fake = [
        OCRResult(text="点击下一节继续", confidence=0.95, bbox=[[10, 10], [120, 10], [120, 32], [10, 32]]),
        OCRResult(text="播放", confidence=0.98, bbox=[[10, 50], [60, 50], [60, 70], [10, 70]]),
        OCRResult(text="请完成验证", confidence=0.99, bbox=[[10, 90], [110, 90], [110, 112], [10, 112]]),
        OCRResult(text="重播", confidence=0.99, bbox=[[10, 130], [60, 130], [60, 150], [10, 150]]),
        OCRResult(text="会活动中的独立主体。只要国家继续存在，爱国主义就有其坚实的基础和丰富",
                  confidence=0.99, bbox=[[10, 170], [900, 170], [900, 200], [10, 200]]),
    ]
    print("匹配结果（验证码类应排最前；'播放'与超长正文中的'继续'不应出现；'重播'为信号类）：")
    for m in matcher.match(fake):
        print(" ", m.to_dict())
