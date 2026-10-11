"""运行产物清理模块。

程序停止时清理无用文件（普通截图、标注图、日志、结果 JSON），
保留验证码截图（captures/need_human/）作为人工处理证据。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("dsh_screen_ocr.cleanup")


def cleanup_artifacts(
    base_dir: str | Path,
    cfg: dict[str, Any] | None = None,
    keep_need_human: bool = True,
) -> int:
    """清理运行产生的无用文件，返回删除的文件数。

    清理范围（目录名取自 config，缺省用默认值）：
    - captures/*.png（普通截图，保留 need_human 子目录）
    - output/annotated/*.png（标注图）
    - output/latest_result.json（结果 JSON）
    - logs/app.log（日志文件）
    """
    base = Path(base_dir)
    out = (cfg or {}).get("output", {}) or {}
    capture_dir = base / out.get("capture_dir", "captures")
    result_dir = base / out.get("result_dir", "output")
    log_dir = base / out.get("log_dir", "logs")
    result_json = out.get("result_json", "latest_result.json")

    removed = 0

    def _unlink(path: Path) -> None:
        nonlocal removed
        try:
            if path.exists() and path.is_file():
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.debug("清理失败 %s: %s", path, exc)

    # 普通截图（不进入 need_human 子目录）
    if capture_dir.exists():
        for f in capture_dir.glob("*.png"):
            _unlink(f)

    # 标注图
    annotated_dir = result_dir / "annotated"
    if annotated_dir.exists():
        for f in annotated_dir.glob("*.png"):
            _unlink(f)

    # 结果 JSON
    _unlink(result_dir / result_json)

    # 日志
    _unlink(log_dir / "app.log")

    if keep_need_human:
        nh = capture_dir / "need_human"
        if nh.exists():
            logger.info("保留验证码截图目录: %s", nh)

    logger.info("已清理 %d 个无用文件", removed)
    return removed
