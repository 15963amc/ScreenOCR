"""入口：ADB 定时截图 + OCR 中文关键词识别（合规学习辅助）。

退出码：
  0 - 正常结束（单次完成 / 循环被 Ctrl+C 中断）
  1 - 一般错误（配置错误、设备错误、OCR 引擎加载失败等）
  2 - 检测到验证码，需要人工处理
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

from adb import ADB, ADBError
from cleanup import cleanup_artifacts
from matcher import Matcher
from ocr import create_engine
from state_machine import StateMachine

logger = logging.getLogger("dsh_screen_ocr")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEED_HUMAN = 2


# --------------------------------------------------------------------- #
# 配置加载
# --------------------------------------------------------------------- #
def load_config(path: str | Path) -> dict[str, Any]:
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return _apply_defaults(cfg)


def _apply_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "adb": {"path": "adb", "remote_dir": "/sdcard"},
        "ocr": {"engine": "rapidocr", "min_confidence": 0.5},
        "keywords": {},
        "match": {"exact_first": True, "enable_contains": True, "max_text_length": 10},
        "state": {
            "consecutive_frames": 3,
            "wait_after_click": 2,
            "click_cooldown": 5,
            "center_tolerance": 30,
            "reset_timeout": 15,
            "gated_keywords": [],
        },
        "loop": {"interval": 5},
        "output": {
            "capture_dir": "captures",
            "log_dir": "logs",
            "result_dir": "output",
            "result_json": "latest_result.json",
            "annotated": True,
        },
    }
    for section, sec_defaults in defaults.items():
        cfg.setdefault(section, {})
        if isinstance(sec_defaults, dict):
            for k, v in sec_defaults.items():
                cfg[section].setdefault(k, v)
    return cfg


# --------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------- #
def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger("dsh_screen_ocr")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_h = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
    file_h.setFormatter(fmt)
    root.addHandler(file_h)


def get_base_dir() -> Path:
    """程序根目录：打包成 EXE 后指向 exe 所在目录，源码运行指向脚本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def build_runtime(
    cfg: dict[str, Any],
    serial: str | None = None,
    ocr_engine: str | None = None,
) -> tuple[ADB, Any, Matcher, StateMachine]:
    """根据配置构建 ADB / OCR / Matcher / StateMachine 运行时。"""
    adb = ADB(
        adb_path=cfg["adb"].get("path", "adb"),
        serial=serial,
        remote_dir=cfg["adb"].get("remote_dir", "/sdcard"),
        tmp_dir=get_base_dir() / ".adb-tmp",
    )
    engine_name = ocr_engine or cfg["ocr"].get("engine", "rapidocr")
    ocr = create_engine(engine_name, cfg["ocr"])
    matcher = Matcher(cfg)
    sm = StateMachine(cfg)
    return adb, ocr, matcher, sm


# --------------------------------------------------------------------- #
# 输出辅助
# --------------------------------------------------------------------- #
def imread_unicode(path: str | Path) -> np.ndarray | None:
    """读取图像（兼容 Windows 含中文/非 ASCII 的绝对路径）。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def imwrite_unicode(path: str | Path, image: np.ndarray) -> bool:
    """写入图像（兼容 Windows 含中文/非 ASCII 的绝对路径）。"""
    ext = Path(path).suffix or ".png"
    ok, buf = cv2.imencode(ext, image)
    if not ok:
        return False
    return bool(buf.tofile(str(path)))


def write_json(result: dict[str, Any], base_dir: Path, cfg: dict[str, Any]) -> Path:
    out_dir = base_dir / cfg["output"]["result_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / cfg["output"]["result_json"]
    with path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.debug("已写入结果: %s", path)
    return path


def save_need_human(image_path: Path, capture_dir: Path, ts_str: str) -> Path:
    nh_dir = capture_dir / "need_human"
    nh_dir.mkdir(parents=True, exist_ok=True)
    dest = nh_dir / f"need_human_{ts_str}.png"
    shutil.copyfile(image_path, dest)
    logger.warning("验证码截图已保存: %s", dest)
    return dest


def draw_annotated(
    image_path: Path,
    image: np.ndarray,
    ocr_results: list[Any],
    matches: list[Any],
    base_dir: Path,
    cfg: dict[str, Any],
    ts_str: str,
) -> Path | None:
    """在截图上画出 OCR 框；验证码红色、下一节/继续绿色、其余蓝色。"""
    canvas = image.copy()
    captcha_texts = {m.text for m in matches if m.category == "captcha"}
    action_texts = {m.text for m in matches if m.category in ("next", "continue")}
    for r in ocr_results:
        pts = np.array(r.bbox, dtype=np.int32).reshape((-1, 1, 2))
        if r.text in captcha_texts:
            color = (0, 0, 255)  # 红
        elif r.text in action_texts:
            color = (0, 255, 0)  # 绿
        else:
            color = (255, 0, 0)  # 蓝
        cv2.polylines(canvas, [pts], True, color, 2)
        x, y = int(pts[0][0][0]), int(pts[0][0][1])
        label = f"{r.text} ({r.confidence:.2f})"
        cv2.putText(canvas, label, (x, max(0, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

    out_dir = base_dir / cfg["output"]["result_dir"] / "annotated"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"annotated_{ts_str}.png"
    imwrite_unicode(out, canvas)
    return out


def verify_after_click(
    adb: ADB,
    ocr: Any,
    matcher: Matcher,
    cfg: dict[str, Any],
    capture_dir: Path,
) -> None:
    """点击后截图验证：确认目标是否已离开屏幕、是否出现验证码。"""
    vpath = capture_dir / f"verify_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    try:
        adb.screenshot(vpath)
        image = imread_unicode(vpath)
        if image is None:
            return
        ocr_results = ocr.recognize(image)
        matches = matcher.match(ocr_results)
        captcha = [m for m in matches if m.category == "captcha"]
        if captcha:
            logger.warning("点击后验证：检测到验证码，请人工处理。")
            return
        remaining = [m for m in matches if m.category in ("next", "continue")]
        if remaining:
            logger.info(
                "点击后验证：目标仍在屏幕（%s），下一帧将继续处理。",
                "、".join(sorted({m.keyword for m in remaining})),
            )
        else:
            logger.info("点击后验证：目标已离开屏幕，点击生效。")
    except ADBError as exc:
        logger.error("点击后验证失败: %s", exc)


# --------------------------------------------------------------------- #
# 单次运行
# --------------------------------------------------------------------- #
def run_once(
    args: argparse.Namespace,
    cfg: dict[str, Any],
    adb: ADB,
    ocr: Any,
    matcher: Matcher,
    sm: StateMachine,
    base_dir: Path,
) -> tuple[dict[str, Any], bool]:
    """执行一次 截图→OCR→匹配→决策，返回 (结果dict, 是否遇验证码)。"""
    timestamp = datetime.now()
    ts_str = timestamp.strftime("%Y%m%d_%H%M%S_%f")
    iso = timestamp.strftime("%Y-%m-%dT%H:%M:%S")

    capture_dir = base_dir / cfg["output"]["capture_dir"]
    capture_dir.mkdir(parents=True, exist_ok=True)
    image_path = capture_dir / f"screen_{ts_str}.png"

    def _error_result(msg: str) -> dict[str, Any]:
        return {
            "timestamp": iso,
            "device": adb.serial or "default",
            "image_path": str(image_path),
            "status": "ERROR",
            "matches": [],
            "all_texts": [],
            "error": msg,
        }

    need_human = False
    try:
        # 1) 截图
        adb.screenshot(image_path)
        image = imread_unicode(image_path)
        if image is None:
            raise RuntimeError("OpenCV 读取截图失败，文件可能损坏")

        # 2) OCR
        ocr_results = ocr.recognize(image)

        # 3) 匹配
        matches = matcher.match(ocr_results)

        # 4) 决策
        decision = sm.decide(matches, args.auto_click)

        # 5) 验证码：保存副本 + 告警，绝不点击
        if decision.status == "NEED_HUMAN":
            need_human = True
            save_need_human(image_path, capture_dir, ts_str)
            logger.warning("【告警】检测到验证码，需要人工处理，已停止一切点击。")

        # 6) 点击（仅下一节/继续播放，且 --auto-click 时）
        if decision.to_click and not need_human:
            for target in decision.to_click:
                cx = int(round(target["center"][0]))
                cy = int(round(target["center"][1]))
                try:
                    adb.tap(cx, cy)
                    logger.info(
                        "点击 [%s] %s @ (%d, %d)",
                        target["category_label"], target["keyword"], cx, cy,
                    )
                except ADBError as exc:
                    logger.error("点击失败: %s", exc)
            wait = float(cfg["state"].get("wait_after_click", 2))
            time.sleep(wait)
            verify_after_click(adb, ocr, matcher, cfg, capture_dir)

        # 7) 组织结果
        all_texts = [r.to_dict() for r in ocr_results]
        match_list = [m.to_dict() for m in matches]
        result: dict[str, Any] = {
            "timestamp": iso,
            "device": adb.serial or "default",
            "image_path": str(image_path),
            "status": decision.status,
            "video_completed": decision.video_completed,
            "matches": match_list,
            "all_texts": all_texts,
        }
        if cfg["output"].get("annotated", True):
            annotated = draw_annotated(image_path, image, ocr_results, matches, base_dir, cfg, ts_str)
            if annotated:
                result["annotated_path"] = str(annotated)

        write_json(result, base_dir, cfg)
        return result, need_human

    except ADBError as exc:
        logger.error("ADB 错误: %s", exc)
        result = _error_result(str(exc))
        write_json(result, base_dir, cfg)
        return result, False
    except Exception as exc:  # 兜底：任何未预期错误都记录为 ERROR
        logger.exception("运行异常: %s", exc)
        result = _error_result(f"{type(exc).__name__}: {exc}")
        write_json(result, base_dir, cfg)
        return result, False


# --------------------------------------------------------------------- #
# 摘要输出
# --------------------------------------------------------------------- #
def print_summary(result: dict[str, Any]) -> None:
    print(f"[{result['status']}] {result['timestamp']} 设备={result['device']}")
    matches = result.get("matches", [])
    if matches:
        for m in matches:
            print(
                f"  - [{m['category_label']}] {m['keyword']!r} 文本={m['text']!r} "
                f"置信度={m['confidence']} 方式={m['match_type']} 中心={m['center']}"
            )
    else:
        print("  无匹配关键词")


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ADB 定时截图 + OCR 中文关键词识别（合规学习辅助）",
        epilog="验证码类关键词仅用于识别并停止，程序绝不自动点击或处理验证码。",
    )
    p.add_argument("--serial", help="目标设备 serial（多设备时必填）")
    p.add_argument("--once", action="store_true", help="单次运行（默认行为）")
    p.add_argument("--loop", action="store_true", help="循环运行，直到 Ctrl+C 或遇验证码")
    p.add_argument("--interval", type=float, default=None, help="循环间隔秒数（覆盖 config）")
    p.add_argument(
        "--auto-click",
        action="store_true",
        help="允许点击下一节/继续播放类中心坐标（默认 dry-run，仅输出不点击）",
    )
    p.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    p.add_argument(
        "--ocr-engine",
        choices=["rapidocr", "paddleocr"],
        default=None,
        help="覆盖 config 中的 OCR 引擎",
    )
    p.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR")
    p.add_argument("--list-devices", action="store_true", help="仅列出在线设备并退出")
    p.add_argument(
        "--no-cleanup",
        action="store_true",
        help="退出时不清理运行产物（默认退出时自动清理，保留验证码截图）",
    )
    return p.parse_args(argv)


def _reconfigure_stdio() -> None:
    """Windows 下将标准输出/错误重配置为 UTF-8，避免 GBK 编码报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _run_cli(args: argparse.Namespace, cfg: dict[str, Any], base_dir: Path) -> int:
    """执行 CLI 主流程，返回退出码。"""
    try:
        adb, ocr, matcher, sm = build_runtime(
            cfg, serial=args.serial, ocr_engine=args.ocr_engine
        )
    except (ImportError, ValueError) as exc:
        logger.error("%s", exc)
        return EXIT_ERROR

    # --list-devices
    if args.list_devices:
        try:
            devices = adb.list_devices()
            print("在线设备:")
            for d in devices:
                print("  -", d)
            return EXIT_OK
        except ADBError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return EXIT_ERROR

    # 解析并校验设备
    try:
        serial = adb.resolve_serial()
        adb.serial = serial
        logger.info("使用设备: %s", serial)
    except ADBError as exc:
        logger.error("%s", exc)
        return EXIT_ERROR

    interval = args.interval if args.interval is not None else cfg["loop"].get("interval", 5)

    if args.loop:
        logger.info("进入循环模式，间隔 %.1fs（Ctrl+C 退出）", interval)
        try:
            while True:
                result, need_human = run_once(args, cfg, adb, ocr, matcher, sm, base_dir)
                print_summary(result)
                if need_human:
                    logger.warning("检测到验证码，循环已退出（退出码 %d）", EXIT_NEED_HUMAN)
                    return EXIT_NEED_HUMAN
                if result["status"] == "ERROR":
                    return EXIT_ERROR
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("收到中断，退出循环。")
            return EXIT_OK
    else:
        result, need_human = run_once(args, cfg, adb, ocr, matcher, sm, base_dir)
        print_summary(result)
        if need_human:
            return EXIT_NEED_HUMAN
        if result["status"] == "ERROR":
            return EXIT_ERROR
        return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    args = parse_args(argv)
    base_dir = get_base_dir()

    try:
        cfg = load_config(base_dir / args.config)
    except Exception as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return EXIT_ERROR

    setup_logging(base_dir / cfg["output"]["log_dir"], args.log_level)

    try:
        return _run_cli(args, cfg, base_dir)
    finally:
        # 停止后自动清理运行产物（保留验证码截图），--no-cleanup 可禁用
        if not getattr(args, "no_cleanup", False):
            try:
                cleanup_artifacts(base_dir, cfg)
            except Exception:
                logger.exception("清理运行产物失败")


if __name__ == "__main__":
    sys.exit(main())
