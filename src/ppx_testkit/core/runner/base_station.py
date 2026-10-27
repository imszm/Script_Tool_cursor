"""受控工位的模板基类。

``execute`` 固定为 setup → run，并在 finally 里复位硬件。信号只在主线程登记，
避免子线程调用 ``signal.signal`` 抛出 ValueError。
"""

from __future__ import annotations

import signal
import threading
from abc import ABC, abstractmethod
from types import FrameType
from typing import Any

from ppx_testkit.core.context import HardwareContext


class BaseStation(ABC):
    """工位基类。子类实现 :meth:`setup` 与 :meth:`run`，不要自行跳过硬件复位。"""

    def __init__(self) -> None:
        self.hw = HardwareContext()
        self._stop_requested = False
        self._previous_signals: dict[int, Any] = {}

    def _bind_stop_signals(self) -> None:
        """在主线程绑定 SIGINT / SIGTERM。子线程或注册失败时静默跳过。"""
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_signals[sig] = signal.signal(sig, self._on_stop_signal)
            except ValueError:
                return

    def _restore_stop_signals(self) -> None:
        """恢复进入 execute 之前的信号处理，避免污染进程里的其他测试。"""
        while self._previous_signals:
            sig, previous = self._previous_signals.popitem()
            try:
                signal.signal(sig, previous)
            except ValueError:
                return

    def _on_stop_signal(self, signum: int, _frame: FrameType | None) -> None:
        """标记停机请求。SIGINT 继续抛出 KeyboardInterrupt，让 finally 能复位。

        :param signum: 信号编号。
        :param _frame: 当前栈帧，处理函数不使用。
        """
        self._stop_requested = True
        if signum == signal.SIGINT:
            raise KeyboardInterrupt

    @abstractmethod
    def setup(self) -> None:
        """打开串口、供电或启动心跳，并登记对应的紧急复位钩子。"""

    @abstractmethod
    def run(self) -> None:
        """执行工位步骤。需要中止时读取 :attr:`_stop_requested`。"""

    def execute(self) -> None:
        """依次调用 setup、run，无论成功、失败或中断都关闭硬件上下文。"""
        self._bind_stop_signals()
        try:
            self.setup()
            self.run()
        finally:
            try:
                self.hw.close_safely()
            finally:
                self._restore_stop_signals()
