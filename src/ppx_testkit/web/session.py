"""同一时间只跑一场测试。停止时先发中断信号，让工位走原来的 teardown。"""

from __future__ import annotations

import logging
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
InterruptFn = Callable[[subprocess.Popen[str]], None]

#: 网页停止时等待工位自行收尾的默认秒数。电机、继电器在通信不畅时的重试收尾要好几秒。
DEFAULT_STOP_GRACE_S = 20.0

_MAX_LINES = 4000
#: 取日志尾部时只读文件末尾这么多字节，避免每秒把整份 full.log 读进内存。
_TAIL_BYTES = 64 * 1024

log = logging.getLogger(__name__)


class RunSession:
    """用独立子进程运行 ppx-test，并把标准输出留在内存里供网页轮询。

    L5 / R3 的 DLL 带有进程级全局状态。每一场测试都新起进程，结束或停止时进程退出，
    句柄和 DLL 状态一起释放。网页进程本身不加载这些 DLL。
    """

    def __init__(self, popen: PopenFactory = subprocess.Popen, interrupt: InterruptFn | None = None) -> None:
        """创建一个空会话。

        :param popen: 启动子进程的工厂，单测注入假进程。
        :param interrupt: 向子进程发停止信号的函数，单测注入，避免对假 pid 调 ``os.kill``。
        """
        self._popen = popen
        self._interrupt = interrupt or _interrupt
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._lines: list[str] = []
        self._base = 0
        self._reader: threading.Thread | None = None
        self.station: str | None = None
        self.started_at: float | None = None
        self.log_root: Path | None = None
        self.exit_code: int | None = None
        self.stop_requested = False
        self.forced = False
        self._cached_full_log: Path | None = None

    def running(self) -> bool:
        """当前子进程是否还在。"""
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self, station: str, args: list[str], log_root: Path) -> None:
        """启动一场测试。已有未结束的进程时抛出 ConfigError。

        正在停止上一场时，先等停止做完再判断。

        :param station: 工位名，只用于状态显示和找日志目录。
        :param args: 完整 argv，第一项应是 Python 解释器。
        :param log_root: ``logs/<工位>`` 目录，用来找本次 full.log。
        """
        with self._stop_lock, self._lock:
            if self.running():
                raise ConfigError(f"已有测试在运行: {self.station}")
            self._lines = []
            self._base = 0
            self.exit_code = None
            self.stop_requested = False
            self.forced = False
            self._cached_full_log = None
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

    def stop(self, grace_s: float = DEFAULT_STOP_GRACE_S) -> None:
        """发一次 Ctrl+Break / SIGINT，等工位收尾；超过宽限期才强制结束。没有在跑的测试时什么也不做。

        每场测试只发一次中断。并发调用时后来的一次等前一次做完，不会再发第二个中断去打断 teardown。
        强制结束时置 :attr:`forced`，网页据此提示到现场确认断电。

        :param grace_s: 等待工位自行退出的秒数。
        """
        with self._stop_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._capture_exit()
                return
            if not self.stop_requested:
                self.stop_requested = True
                try:
                    self._interrupt(proc)
                except OSError as exc:
                    log.warning("向工位 %s 发送停止信号失败: %s", self.station, exc)
            try:
                proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.forced = True
                log.error(
                    "工位 %s 在 %.0f 秒内没有退出，已强制结束。硬件复位可能没有做完，请到现场确认继电器/电机已断电。",
                    self.station, grace_s,
                )
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            self._capture_exit()

    def snapshot(self) -> dict[str, Any]:
        """当前状态。exit_code 在进程结束后才有值。

        运行中不读 full.log 尾部。结束后才取最后若干行，供页面补上控制台没接到的收尾日志。

        :return: 运行状态、启动时刻、退出码、是否已请求停止、是否被强制结束，以及本次 full.log 的位置和尾部。
        """
        self._capture_exit()
        running = self.running()
        full = self.latest_full_log()
        return {
            "running": running,
            "station": self.station,
            "started_at": self.started_at,
            "exit_code": self.exit_code,
            "stop_requested": self.stop_requested,
            "forced": self.forced,
            "full_log": str(full) if full else None,
            "full_tail": [] if running or full is None else _tail(full, 40),
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
        """本次启动之后新建的 full.log。还没写出来时返回 None。

        找到后记在本场会话里。下一场 ``start`` 会清掉，避免每次轮询都扫日志目录。
        """
        started = self.started_at
        cached = self._cached_full_log
        if cached is not None and started is not None:
            if _log_is_current(cached, started):
                return cached
            self._cached_full_log = None
        root = self.log_root
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
                if child.is_dir() and _log_is_current(full, started):
                    found.append(full)
            except OSError:
                continue
        if not found:
            return None
        chosen = max(found, key=lambda path: path.stat().st_mtime)
        self._cached_full_log = chosen
        return chosen

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
    """让子进程走 KeyboardInterrupt，从而执行 HardwareContext.close_safely。

    Windows 上 Ctrl+C 会打到整个控制台，包括网页进程，所以对新建的进程组发 Ctrl+Break。
    子进程在 ``cli`` 里把 SIGBREAK 转成 KeyboardInterrupt。其他系统直接发 SIGINT。
    """
    if os.name == "nt":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        return
    os.kill(proc.pid, signal.SIGINT)


def _log_is_current(path: Path, started: float) -> bool:
    """文件是否像本场测试写出的 full.log。

    :param path: 候选日志。
    :param started: 本场启动的 ``time.time()`` 秒。
    :return: 是普通文件且修改时间不早于启动前 2 秒。
    """
    try:
        return path.is_file() and path.stat().st_mtime >= started - 2
    except OSError:
        return False


def _tail(path: Path, limit: int) -> list[str]:
    """读文件最后 ``limit`` 行，只碰末尾约 64 KB。

    从末尾往前读时，开头可能切在一行中间，这一截丢掉。文件更短时整段解码。

    :param path: 日志文件。
    :param limit: 最多返回的行数。
    :return: 末尾若干行。打不开文件或 limit 小于 1 时返回空列表。
    """
    if limit < 1:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            chunk = handle.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]
    return lines[-limit:]
