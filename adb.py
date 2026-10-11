"""ADB 设备连接与截图模块。

设计要点：
- 截图严格采用两步法：``adb shell screencap -p <remote>`` 在设备端生成 PNG，
  再用 ``adb pull <remote> <local>`` 拉取到本地。避免 Windows 下将 adb 的
  stdout 重定向到文件造成的二进制损坏（换行符 \\r\\n 污染 PNG 数据）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("dsh_screen_ocr.adb")


class ADBError(RuntimeError):
    """ADB 相关错误。"""


class ADB:
    def __init__(
        self,
        adb_path: str,
        serial: str | None = None,
        remote_dir: str = "/sdcard",
        timeout: int = 30,
        tmp_dir: str | Path | None = None,
    ) -> None:
        self.adb_path = adb_path
        self.serial = serial
        self.remote_dir = remote_dir.rstrip("/")
        self.timeout = timeout
        # adb daemon 日志临时目录：若系统 Temp 不可写（如受限沙箱），
        # 重定向 TEMP/TMP 到项目目录，避免 "cannot open adb.log: Permission denied"
        self.tmp_dir: Path | None = Path(tmp_dir) if tmp_dir else None
        if self.tmp_dir:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    def _build_cmd(self, *args: str) -> list[str]:
        cmd = [self.adb_path]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        return cmd

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.tmp_dir:
            env["TEMP"] = str(self.tmp_dir)
            env["TMP"] = str(self.tmp_dir)
        return env

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """执行一条 adb 命令，返回 CompletedProcess（stdout/stderr 为文本）。"""
        cmd = self._build_cmd(*args)
        logger.debug("adb 执行: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                encoding="utf-8",
                errors="replace",
                env=self._env(),
            )
        except FileNotFoundError as exc:
            raise ADBError(f"找不到 adb 可执行文件: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ADBError(f"adb 命令超时({self.timeout}s): {' '.join(cmd)}") from exc

        if check and proc.returncode != 0:
            raise ADBError(
                f"adb 命令失败(exit={proc.returncode}): {' '.join(cmd)}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )
        return proc

    # ------------------------------------------------------------------ #
    def list_devices(self) -> list[str]:
        """返回处于 'device' 状态的设备 serial 列表。"""
        proc = self.run("devices", check=False)
        if proc.returncode != 0:
            raise ADBError(f"adb devices 执行失败: {proc.stderr.strip()}")
        devices: list[str] = []
        for line in proc.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            serial = parts[0]
            state = parts[1] if len(parts) > 1 else ""
            if state == "device":
                devices.append(serial)
            else:
                logger.warning("设备 %s 状态为 %s，已跳过", serial, state or "unknown")
        return devices

    def resolve_serial(self) -> str:
        """解析实际使用的 serial：显式指定优先，否则单设备自动选择。"""
        if self.serial:
            devices = self.list_devices()
            if self.serial not in devices:
                raise ADBError(
                    f"指定设备 {self.serial} 未连接。当前在线设备: {devices}"
                )
            return self.serial
        devices = self.list_devices()
        if not devices:
            raise ADBError("未检测到在线设备。请连接手机并开启 USB 调试。")
        if len(devices) > 1:
            raise ADBError(
                f"检测到多台设备 {devices}，请通过 --serial 指定其中一台。"
            )
        return devices[0]

    # ------------------------------------------------------------------ #
    def screenshot(self, local_path: Path) -> Path:
        """截取屏幕并 pull 到本地，返回本地文件路径。"""
        remote = f"{self.remote_dir}/dsh_screen_{int(time.time() * 1000)}.png"
        # 1) 在设备端生成 PNG（避免 stdout 二进制重定向损坏）
        self.run("shell", "screencap", "-p", remote)
        try:
            # 2) pull 到本地
            local_path.parent.mkdir(parents=True, exist_ok=True)
            self.run("pull", remote, str(local_path))
        finally:
            # 清理设备端临时文件（失败不阻断流程）
            self.run("shell", "rm", "-f", remote, check=False)

        if not local_path.exists() or local_path.stat().st_size == 0:
            raise ADBError(f"截图文件异常（不存在或为空）: {local_path}")
        return local_path

    def tap(self, x: int, y: int) -> None:
        """点击屏幕坐标 (x, y)。"""
        self.run("shell", "input", "tap", str(int(x)), str(int(y)))
        logger.info("已点击坐标 (%d, %d)", int(x), int(y))
