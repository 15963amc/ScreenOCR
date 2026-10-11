# DSH 屏幕关键词识别（ADB + OCR）

通过 ADB 连接安卓设备，定时截图，用 OCR 识别屏幕上的中文关键词并输出 JSON 结果。

> **合规声明**：本项目仅用于你拥有授权的设备与合规学习辅助。程序**不生成**任何绕过验证码、
> 自动填写验证码、破解接口、伪造学习记录的代码。验证码类关键词**只用于识别并停止**，绝不自动点击或处理。

## 功能概览

- ADB 设备连接：自动 `adb devices` 检查，支持 `--serial` 指定设备
- 截图：`adb shell screencap -p` + `adb pull` 两步法，避免 Windows 二进制重定向损坏
- OCR：默认 RapidOCR（轻量），可选 PaddleOCR
- 关键词匹配：优先级「验证码类 > 下一节类 > 继续播放类」，精确匹配优先、其次包含匹配
- 安全点击：默认 dry-run，`--auto-click` 才点击；连续 2~3 帧命中、5 秒冷却、点击后 2 秒验证
- 验证码：`NEED_HUMAN` 状态、保存截图（含 `need_human`）、告警、退出码 2、**永不点击**
- 输出：`latest_result.json` + `annotated_*.png` 标注图 + 控制台/文件日志

## 项目结构

```
.
├── app.py             # 统一入口：命令行走 CLI，双击打开图形界面
├── main.py            # CLI 入口、主循环、JSON 输出、日志、annotated
├── gui.py             # tkinter 图形界面（设备选择、启停、实时日志、停止清理）
├── cleanup.py         # 运行产物清理（保留验证码截图）
├── adb.py             # ADB 设备连接、截图、点击
├── ocr.py             # OCR 抽象层（RapidOCR 默认 / PaddleOCR 可选）
├── matcher.py         # 关键词匹配（含无设备自测）
├── state_machine.py   # 连续帧确认、冷却、验证码停止、重播门控
├── pack.py            # PyInstaller 打包脚本（onedir）
├── config.yaml        # 全部配置集中管理
├── requirements.txt
└── README.md
```

## 环境要求

- Python 3.10+
- ADB（platform-tools），路径在 `config.yaml` 中配置
- 安卓设备已开启「USB 调试」，并通过 USB 或无线 ADB 连接

## 安装

```bash
# 创建虚拟环境（推荐）
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 可选：PaddleOCR

如需使用 PaddleOCR（精度更高、体积更大）：

```bash
pip install paddleocr paddlepaddle
```

并把 `config.yaml` 中 `ocr.engine` 改为 `paddleocr`，或用 `--ocr-engine paddleocr`。

## 配置

所有关键词、阈值、路径都集中在 [config.yaml](config.yaml)。关键项：

| 配置 | 说明 |
| --- | --- |
| `adb.path` | ADB 可执行文件路径 |
| `ocr.min_confidence` | 全局置信度阈值，低于此值丢弃 |
| `keywords.*.words` | 三组关键词，可 `enabled: false` 关闭 |
| `keywords.continue.ambiguous_words` | 「播放」等易误匹配词，单独开关与更高阈值 |
| `keywords.signal.video_complete` | 视频完成信号词（如「重播」），不作点击目标，只解锁门控 |
| `match.max_text_length` | contains 匹配仅对长度 ≤ 该值的 OCR 文本生效，避免正文长句误报（默认 10） |
| `state.consecutive_frames` | 连续多少帧命中才确认（2~3） |
| `state.gated_keywords` | 需先检测到视频完成信号（「重播」）才允许点击的关键词（如「下一篇」） |
| `state.click_cooldown` | 同一目标点击冷却秒数 |
| `state.wait_after_click` | 点击后等待秒数再验证 |

## 运行

### 1. 检查设备

```bash
python main.py --list-devices
```

### 2. 单次运行（默认 dry-run，只输出不点击）

```bash
python main.py --once
```

### 3. 循环运行

```bash
python main.py --loop --interval 5
```

### 4. 允许点击（下一节 / 继续播放类）

```bash
python main.py --once --auto-click
python main.py --loop --interval 5 --auto-click
```

### 5. 指定设备 / 引擎 / 日志级别

```bash
python main.py --once --serial 192.168.1.100:5555
python main.py --once --ocr-engine paddleocr --log-level DEBUG
```

> 说明：屏幕坐标与截图像素坐标一致（`screencap -p` 输出物理分辨率，`input tap` 使用物理坐标），
> OCR 框中心点可直接作为点击坐标。若设备有缩放/刘海差异，请先以 dry-run 核对 `latest_result.json`
> 中的坐标与实际位置再开启 `--auto-click`。

## 三平台示例

### Windows

```powershell
# 1. 激活虚拟环境
.venv\Scripts\activate
# 2. 确认 config.yaml 中 adb.path 指向你的 adb.exe（默认 C:\games\platform-tools\adb.exe）
# 3. 检查设备
python main.py --list-devices
# 4. 运行
python main.py --once
```

### macOS

```bash
source .venv/bin/activate
# 安装 adb：brew install android-platform-tools
# 将 config.yaml 中 adb.path 改为 "adb" 或 /opt/homebrew/bin/adb
python main.py --list-devices
python main.py --loop --interval 5
```

### Linux

```bash
source .venv/bin/activate
# 安装 adb：sudo apt install adb  或 下载 platform-tools 并配置路径
# 将 config.yaml 中 adb.path 改为 "adb" 或 /usr/bin/adb
python main.py --list-devices
python main.py --once --auto-click
```

## 调试

1. **无设备测试关键词匹配**（不需要手机/ADB）：

   ```bash
   python matcher.py
   ```

2. **查看日志**：`logs/app.log` 与控制台同步输出；`--log-level DEBUG` 可查看 adb 命令、匹配细节。

3. **标注图**：`output/annotated/annotated_*.png` 画出所有 OCR 框，
   红色=验证码类，绿色=下一节/继续播放类，蓝色=其他文本。

4. **结果 JSON**：`output/latest_result.json` 为每次识别的最新结果，含 `matches` 与 `all_texts`。

5. **验证码**：命中验证码后截图复制到 `captures/need_human/`，文件名为 `need_human_*.png`。

## 输出 JSON 结构

```json
{
  "timestamp": "2026-10-10T12:00:00",
  "device": "xxx",
  "image_path": "captures/screen_xxx.png",
  "status": "MATCHED",
  "matches": [
    {
      "category": "next",
      "category_label": "下一节类",
      "keyword": "下一节",
      "text": "下一节",
      "confidence": 0.98,
      "match_type": "exact",
      "bbox": [[10, 10], [100, 10], [100, 30], [10, 30]],
      "center": [55.0, 20.0]
    }
  ],
  "all_texts": [],
  "annotated_path": "output/annotated/annotated_xxx.png"
}
```

`status` 取值：`MATCHED`（命中非验证码关键词）、`NEED_HUMAN`（命中验证码）、`NO_MATCH`（无命中）、`ERROR`（异常）。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常结束（单次完成 / 循环被 Ctrl+C 中断） |
| 1 | 一般错误（配置/设备/OCR 引擎加载失败等） |
| 2 | 检测到验证码，需要人工处理 |

## 验收命令与预期输出

```bash
# 1) 语法自检
python -m py_compile main.py gui.py app.py cleanup.py adb.py ocr.py matcher.py state_machine.py

# 2) 无设备自测匹配（预期打印 3 条匹配，验证码类排最前，"播放"不出现）
python matcher.py

# 3) 检查设备（预期列出在线设备或给出明确错误）
python main.py --list-devices

# 4) 单次 dry-run（预期生成 captures/screen_*.png、output/latest_result.json，
#    命中关键词时打印 [MATCHED] 及匹配明细，且不点击）
python main.py --once

# 5) 循环（预期每 interval 秒输出一次；命中验证码时打印告警、保存 need_human 截图并以退出码 2 退出）
python main.py --loop --interval 5
```

## 打包成 EXE（图形界面 + 命令行）

项目内置 [gui.py](gui.py)（tkinter 图形界面）、[app.py](app.py)（统一入口）、
[cleanup.py](cleanup.py)（运行产物清理）、[pack.py](pack.py)（打包脚本）。

### 打包

```bash
pip install pyinstaller
py pack.py
```

产物在 `dist/ScreenOCR/` 目录，内含 `ScreenOCR.exe` 与 `config.yaml`（用户可编辑 adb 路径等）。
首次打包约需几分钟，体积约 250MB（含 OCR 模型与推理引擎）。

### 使用方式

- **图形界面**：双击 `ScreenOCR.exe`（无参数），打开窗口：
  - 选择设备、单次/循环、间隔、是否自动点击
  - 「启动」开始、「停止」停止
  - 实时日志显示
- **命令行**：`ScreenOCR.exe` 后接参数，等价于 `python main.py <参数>`：

  ```bash
  ScreenOCR.exe --list-devices
  ScreenOCR.exe --once
  ScreenOCR.exe --loop --interval 5 --auto-click
  ```

### 停止后自动清理

程序停止时（循环退出、点击「停止」、关闭窗口、命令行退出）自动清理运行产物——
普通截图、标注图、日志、结果 JSON，**保留验证码截图**（`captures/need_human/`）作为证据。

- 命令行加 `--no-cleanup` 可禁用本次清理；
- 图形界面勾选「停止后不清理运行产物」可禁用。

## 免责声明

请仅在你拥有合法授权的设备与学习场景中使用，并遵守相关平台的服务条款与当地法律法规。
