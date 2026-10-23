"""同一时间只跑一场测试。停止时先发中断信号，让工位走原来的 teardown。"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ppx_testkit.exceptions import ConfigError
from ppx_testkit.utils.paths import project_root

PopenFactory = Callable[..., subprocess.Popen[str]]

_MAX_LINES = 4000


class RunSession:
    """子进程运行 ppx-test，并把标准输出留在内存里供网页轮询。"""

    def __init__(self, popen: PopenFactory = subprocess.Popen) -> None:
        self._popen = popen
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._lines: list[str] = []
        self._base = 0
        self._reader: threading.Thread | None = None
        self.station: str | None = None
        self.started_at: float | None = None
        self.log_root: Path | None = None
        self.exit_code: int | None = None

    def running(self) -> bool:
        """当前子进程是否还在。"""
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self, station: str, args: list[str], log_root: Path) -> None:
        """启动一场测试。已有未结束的进程时抛出 ConfigError。

        :param station: 工位名，只用于状态显示和找日志目录。
        :param args: 完整 argv，第一项应是 Python 解释器。
        :param log_root: ``logs/<工位>`` 目录，用来找本次 full.log。
        """
        with self._lock:
            if self.running():
                raise ConfigError(f"已有测试在运行: {self.station}")
            self._lines = []
            self._base = 0
            self.exit_code = None
            self.station = station
            self.log_root = log_root
            self.started_at = time.time()
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUTF8", "1")
            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": env,
                "cwd": str(project_root()),
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            self._proc = self._popen(args, **kwargs)
            self._reader = threading.Thread(target=self._pump, name="ppx-web-log", daemon=True)
            self._reader.start()

    def stop(self, grace_s: float = 5.0) -> None:
        """先发 Ctrl+Break / SIGINT，超时后再结束进程。没有在跑的测试时什么也不做。"""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._capture_exit()
            return
        _interrupt(proc)
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        self._capture_exit()

    def snapshot(self) -> dict[str, Any]:
        """当前状态。exit_code 在进程结束后才有值。"""
        self._capture_exit()
        full = self.latest_full_log()
        return {
            "running": self.running(),
            "station": self.station,
            "exit_code": self.exit_code,
            "full_log": str(full) if full else None,
            "full_tail": _tail(full, 40) if full else [],
        }

    def log_from(self, offset: int) -> dict[str, Any]:
        """从 offset 起返回新的控制台行。offset 小于已丢弃的部分时从现存的第一行开始。

        :return: ``{"next": 下一次 offset, "lines": [...]}``。
        """
        with self._lock:
            if offset < self._base:
                offset = self._base
            index = offset - self._base
            lines = self._lines[index:]
            return {"next": self._base + len(self._lines), "lines": lines}

    def latest_full_log(self) -> Path | None:
        """本次启动之后新建的 full.log。还没写出来时返回 None。"""
        root = self.log_root
        started = self.started_at
        if root is None or started is None or not root.is_dir():
            return None
        found: list[Path] = []
        try:
            children = list(root.iterdir())
        except OSError:
            return None
        for child in children:
            full = child / "full.log"
            try:
                if child.is_dir() and full.is_file() and full.stat().st_mtime >= started - 2:
                    found.append(full)
            except OSError:
                continue
        if not found:
            return None
        return max(found, key=lambda path: path.stat().st_mtime)

    def _capture_exit(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is not None:
            self.exit_code = proc.returncode

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            text = line.rstrip("\r\n")
            with self._lock:
                self._lines.append(text)
                extra = len(self._lines) - _MAX_LINES
                if extra > 0:
                    del self._lines[:extra]
                    self._base += extra
        self._capture_exit()


def _interrupt(proc: subprocess.Popen[str]) -> None:
    """让子进程收到键盘中断。Windows 用 Ctrl+Break，其他系统用 SIGINT。"""
    if os.name == "nt":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        return
    os.kill(proc.pid, signal.SIGINT)


def _tail(path: Path, limit: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-limit:]
