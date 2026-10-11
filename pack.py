"""打包脚本：用 PyInstaller 打包成 onedir 文件夹（含 exe + 依赖 + OCR 模型）。

用法：py pack.py
产物：dist/ScreenOCR/ 目录，内含 ScreenOCR.exe 与 config.yaml
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import rapidocr
from PyInstaller.__main__ import run

HERE = Path(__file__).resolve().parent
rapidocr_dir = Path(rapidocr.__file__).resolve().parent
SEP = ";" if sys.platform == "win32" else ":"

# rapidocr 运行所需的数据文件（用 __file__ 定位，必须随包打包）
datas = [
    f"{rapidocr_dir / 'config.yaml'}{SEP}rapidocr",
    f"{rapidocr_dir / 'default_models.yaml'}{SEP}rapidocr",
    f"{rapidocr_dir / 'models'}{SEP}rapidocr/models",
]

args = [
    "--onedir",
    "--name", "ScreenOCR",
    "--console",
    "--noconfirm",
    "--clean",
    str(HERE / "app.py"),
]
for d in datas:
    args += ["--add-data", d]

run(args)

# 把项目 config.yaml 复制到 exe 同目录（用户可编辑 adb 路径等）
dist_cfg = HERE / "dist" / "ScreenOCR" / "config.yaml"
shutil.copyfile(HERE / "config.yaml", dist_cfg)
print(f"打包完成，config.yaml 已复制到 {dist_cfg}")
