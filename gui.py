"""tkinter 图形界面。

提供设备选择、单次/循环运行、间隔与自动点击设置、启动/停止按钮、
实时日志显示；停止后自动清理运行产物（保留验证码截图）。
"""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

from adb import ADB, ADBError
from cleanup import cleanup_artifacts
from main import build_runtime, get_base_dir, load_config, run_once

logger = logging.getLogger("dsh_screen_ocr")


class GuiLogHandler(logging.Handler):
    """把日志转发到队列，供 GUI 界面显示。"""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(self.format(record))
        except Exception:
            pass


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.base_dir: Path = get_base_dir()
        self.cfg = load_config(self.base_dir / "config.yaml")

        self.log_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.runtime = None

        self._build_ui()
        self._setup_logging()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll_logs)
        self.refresh_devices()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        self.root.title("屏幕关键词识别（ADB + OCR）")
        self.root.geometry("760x620")
        self.root.minsize(680, 520)

        pad = {"padx": 8, "pady": 4}

        # 设备区
        dev_frame = ttk.LabelFrame(self.root, text="设备")
        dev_frame.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(dev_frame, text="设备 serial:").grid(row=0, column=0, sticky="w", **pad)
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(dev_frame, textvariable=self.device_var, state="readonly", width=28)
        self.device_combo.grid(row=0, column=1, sticky="we", **pad)
        ttk.Button(dev_frame, text="刷新", command=self.refresh_devices).grid(row=0, column=2, **pad)
        dev_frame.columnconfigure(1, weight=1)

        # 参数区
        param_frame = ttk.LabelFrame(self.root, text="运行参数")
        param_frame.pack(fill="x", padx=10, pady=4)

        self.mode_var = tk.StringVar(value="once")
        ttk.Label(param_frame, text="模式:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Radiobutton(param_frame, text="单次", variable=self.mode_var, value="once").grid(row=0, column=1, sticky="w", **pad)
        ttk.Radiobutton(param_frame, text="循环", variable=self.mode_var, value="loop").grid(row=0, column=2, sticky="w", **pad)

        ttk.Label(param_frame, text="间隔(秒):").grid(row=0, column=3, sticky="w", **pad)
        self.interval_var = tk.StringVar(value=str(self.cfg.get("loop", {}).get("interval", 5)))
        ttk.Entry(param_frame, textvariable=self.interval_var, width=6).grid(row=0, column=4, sticky="w", **pad)

        self.auto_click_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(param_frame, text="自动点击（下一节/继续播放类）", variable=self.auto_click_var).grid(
            row=1, column=0, columnspan=3, sticky="w", **pad
        )
        self.no_cleanup_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(param_frame, text="停止后不清理运行产物", variable=self.no_cleanup_var).grid(
            row=1, column=3, columnspan=2, sticky="w", **pad
        )

        # 按钮区
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=10, pady=4)
        self.btn_start = ttk.Button(btn_frame, text="启动", command=self.start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(btn_frame, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        ttk.Label(btn_frame, text="验证码类关键词只识别并停止，绝不自动点击。").pack(side="right", padx=4)

        # 日志区
        log_frame = ttk.LabelFrame(self.root, text="日志")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=18, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    def _setup_logging(self) -> None:
        log_dir = self.base_dir / self.cfg["output"]["log_dir"]
        log_dir.mkdir(parents=True, exist_ok=True)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        root = logging.getLogger("dsh_screen_ocr")
        root.setLevel(logging.INFO)
        root.handlers.clear()
        root.propagate = False

        gui_h = GuiLogHandler(self.log_queue)
        gui_h.setFormatter(fmt)
        root.addHandler(gui_h)

        file_h = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
        file_h.setFormatter(fmt)
        root.addHandler(file_h)

    # ------------------------------------------------------------------ #
    def log(self, msg: str) -> None:
        self.log_queue.put(msg)

    def _poll_logs(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_logs)

    # ------------------------------------------------------------------ #
    def refresh_devices(self) -> None:
        try:
            adb = ADB(
                self.cfg["adb"].get("path", "adb"),
                tmp_dir=self.base_dir / ".adb-tmp",
            )
            devices = adb.list_devices()
            self.device_combo["values"] = devices
            if devices:
                self.device_combo.current(0)
                self.log(f"检测到 {len(devices)} 台设备: {', '.join(devices)}")
            else:
                self.log("未检测到在线设备，请连接手机并开启 USB 调试")
        except ADBError as exc:
            self.log(f"检测设备失败: {exc}")

    def start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        serial = self.device_var.get().strip() or None
        mode = self.mode_var.get()
        try:
            interval = float(self.interval_var.get())
        except ValueError:
            interval = self.cfg.get("loop", {}).get("interval", 5)
        auto_click = self.auto_click_var.get()
        no_cleanup = self.no_cleanup_var.get()

        self.log(f"启动：模式={mode} 间隔={interval}s 自动点击={auto_click} 设备={serial or '自动'}")
        self.worker = threading.Thread(
            target=self._worker_loop,
            args=(serial, mode, interval, auto_click, no_cleanup),
            daemon=True,
        )
        self.worker.start()

    def stop(self) -> None:
        self.log("收到停止请求，等待当前帧完成…")
        self.stop_event.set()
        self.btn_stop.config(state="disabled")

    def on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno("退出", "任务正在运行，确定停止并退出吗？"):
                return
            self.stop_event.set()
            self.root.after(200, self._wait_and_close)
        else:
            self._do_cleanup_on_exit()
            self.root.destroy()

    def _wait_and_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.root.after(200, self._wait_and_close)
        else:
            self._do_cleanup_on_exit()
            self.root.destroy()

    def _do_cleanup_on_exit(self) -> None:
        if not self.no_cleanup_var.get():
            try:
                cleanup_artifacts(self.base_dir, self.cfg)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    def _worker_loop(
        self,
        serial: str | None,
        mode: str,
        interval: float,
        auto_click: bool,
        no_cleanup: bool,
    ) -> None:
        try:
            adb, ocr, matcher, sm = build_runtime(self.cfg, serial=serial)
            serial = adb.resolve_serial()
            adb.serial = serial
            logger.info("使用设备: %s", serial)
            self.runtime = (adb, ocr, matcher, sm)
        except Exception as exc:
            logger.error("初始化失败: %s", exc)
            self.root.after(0, self._on_worker_done)
            return

        args_ns = SimpleNamespace(auto_click=auto_click)

        try:
            if mode == "once":
                result, need_human = run_once(args_ns, self.cfg, adb, ocr, matcher, sm, self.base_dir)
                if need_human:
                    logger.warning("检测到验证码，需要人工处理，已停止。")
                elif result["status"] == "ERROR":
                    logger.error("运行出错: %s", result.get("error", "未知错误"))
            else:
                while not self.stop_event.is_set():
                    result, need_human = run_once(args_ns, self.cfg, adb, ocr, matcher, sm, self.base_dir)
                    if need_human:
                        logger.warning("检测到验证码，需要人工处理，已停止。")
                        break
                    if result["status"] == "ERROR":
                        logger.error("运行出错，停止循环。")
                        break
                    if self.stop_event.wait(interval):
                        break
        except Exception as exc:
            logger.exception("运行异常: %s", exc)
        finally:
            if not no_cleanup:
                try:
                    cleanup_artifacts(self.base_dir, self.cfg)
                except Exception:
                    logger.exception("清理运行产物失败")
            logger.info("任务已停止")
            self.root.after(0, self._on_worker_done)

    def _on_worker_done(self) -> None:
        self.worker = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
