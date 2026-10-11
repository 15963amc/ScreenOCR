"""OCR 抽象层：RapidOCR（默认，轻量）/ PaddleOCR（可选）。

统一输出 OCRResult，包含文本、置信度、四点 bbox、中心点 center。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np

logger = logging.getLogger("dsh_screen_ocr.ocr")


@dataclass
class OCRResult:
    """单条 OCR 识别结果。"""

    text: str
    confidence: float
    bbox: list[list[float]]  # 四点坐标 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
    center: tuple[float, float] = field(init=False)

    def __post_init__(self) -> None:
        xs = [float(p[0]) for p in self.bbox]
        ys = [float(p[1]) for p in self.bbox]
        self.center = (sum(xs) / len(xs), sum(ys) / len(ys))

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": round(float(self.confidence), 4),
            "bbox": [[round(float(x), 2), round(float(y), 2)] for x, y in self.bbox],
            "center": [round(float(self.center[0]), 2), round(float(self.center[1]), 2)],
        }


class BaseOCR(ABC):
    """OCR 引擎抽象基类。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.min_confidence = float(config.get("min_confidence", 0.5))

    @abstractmethod
    def recognize(self, image: np.ndarray) -> list[OCRResult]:
        """识别一张 BGR 图像，返回按置信度过滤后的 OCRResult 列表。"""


class RapidOCREngine(BaseOCR):
    """RapidOCR（rapidocr-onnxruntime）引擎。"""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._engine = self._load()

    def _load(self) -> Any:
        try:
            from rapidocr import RapidOCR  # v2.x / v3.x
        except ImportError:
            try:
                from rapidocr_onnxruntime import RapidOCR  # 旧版 1.2.x
            except ImportError as exc:
                raise ImportError(
                    "未安装 RapidOCR，请执行: pip install rapidocr onnxruntime"
                ) from exc
        logger.info("RapidOCR 引擎已加载")
        return RapidOCR()

    def recognize(self, image: np.ndarray) -> list[OCRResult]:
        results: list[OCRResult] = []
        try:
            output = self._engine(image)
        except Exception as exc:  # 引擎内部异常不阻断主流程
            logger.exception("RapidOCR 推理失败: %s", exc)
            return results

        # rapidocr 3.x 返回 RapidOCROutput；旧版返回 (result, elapse)
        if hasattr(output, "boxes") and hasattr(output, "txts") and hasattr(output, "scores"):
            boxes, txts, scores = output.boxes, output.txts, output.scores
            if boxes is None or txts is None or scores is None:
                return results
            for box, text, score in zip(boxes, txts, scores):
                self._append(results, box, text, score)
            return results

        try:
            raw, _elapse = output
        except (TypeError, ValueError):
            logger.warning("无法解析 RapidOCR 返回结果: %r", output)
            return results
        if not raw:
            return results
        for item in raw:
            try:
                box, text, score = item[0], item[1], float(item[2])
            except (IndexError, TypeError, ValueError) as exc:
                logger.warning("OCR 结果格式异常，已跳过: %r (%s)", item, exc)
                continue
            self._append(results, box, text, score)
        return results

    def _append(self, results: list[OCRResult], box: Any, text: Any, score: Any) -> None:
        conf = float(score)
        if conf < self.min_confidence:
            return
        bbox = [[float(p[0]), float(p[1])] for p in box]
        results.append(OCRResult(text=str(text).strip(), confidence=conf, bbox=bbox))


class PaddleOCREngine(BaseOCR):
    """PaddleOCR 引擎（可选，需自行安装 paddleocr / paddlepaddle）。"""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise ImportError(
                "未安装 PaddleOCR，请执行: pip install paddleocr paddlepaddle"
            ) from exc
        logger.info("PaddleOCR 引擎已加载")
        self._engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

    def recognize(self, image: np.ndarray) -> list[OCRResult]:
        results: list[OCRResult] = []
        try:
            raw = self._engine.ocr(image, cls=True)
        except Exception as exc:
            logger.exception("PaddleOCR 推理失败: %s", exc)
            return results
        if not raw or not raw[0]:
            return results
        for item in raw[0]:
            try:
                box, (text, score) = item[0], item[1]
                conf = float(score)
            except (IndexError, TypeError, ValueError) as exc:
                logger.warning("OCR 结果格式异常，已跳过: %r (%s)", item, exc)
                continue
            if conf < self.min_confidence:
                continue
            bbox = [[float(p[0]), float(p[1])] for p in box]
            results.append(OCRResult(text=str(text).strip(), confidence=conf, bbox=bbox))
        return results


def create_engine(name: str, config: dict[str, Any]) -> BaseOCR:
    """工厂函数：根据名称创建 OCR 引擎。"""
    name = (name or "rapidocr").strip().lower()
    if name in ("rapidocr", "rapid"):
        return RapidOCREngine(config)
    if name in ("paddleocr", "paddle"):
        return PaddleOCREngine(config)
    raise ValueError(f"未知 OCR 引擎: {name}（可选: rapidocr | paddleocr）")
