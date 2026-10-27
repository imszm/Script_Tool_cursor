"""硬件资源的统一生命周期。

串口、继电器、心跳等上下文交给 ``ExitStack`` 托管。紧急复位钩子单独按注册逆序执行，
某个钩子失败只记日志，不跳过后面的断电动作和句柄释放。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from typing import TypeVar

from ppx_testkit.logger import get_logger

T = TypeVar("T")

logger = get_logger(__name__)


class HardwareContext:
    """托管硬件上下文，并在退出时先做紧急复位。

    支持 ``with HardwareContext() as hw``。``close_safely`` 可重复调用，第二次起不再执行钩子。
    """

    def __init__(self) -> None:
        self._stack = ExitStack()
        self._stops: list[tuple[str, Callable[[], None]]] = []
        self._closed = False

    def enter_resource(self, cm: AbstractContextManager[T]) -> T:
        """把上下文资源交给 ExitStack，退出时自动调用其 ``__exit__``。

        :param cm: 实现 ``__enter__`` / ``__exit__`` 的对象，例如已封装的串口。
        :return: ``cm.__enter__`` 的返回值。
        :raises RuntimeError: 上下文已经关闭。
        """
        if self._closed:
            raise RuntimeError("HardwareContext 已关闭，不能再托管资源")
        return self._stack.enter_context(cm)

    def register_emergency_stop(self, callback: Callable[[], None], name: str = "") -> None:
        """登记安全停机动作。关闭时按注册逆序（LIFO）执行。

        应按启动顺序登记，例如先停心跳、再释能、再断继电器；实际执行时后登记的先跑。

        :param callback: 无参可调用对象，如继电器断电或 ``heartbeat.stop``。
        :param name: 日志里的钩子名；空字符串时改用函数名。
        :raises RuntimeError: 上下文已经关闭。
        """
        if self._closed:
            raise RuntimeError("HardwareContext 已关闭，不能再登记紧急复位")
        self._stops.append((name, callback))

    def close_safely(self) -> None:
        """先逆序跑完紧急复位，再 ``ExitStack.close()`` 释放底层句柄。

        每个钩子单独捕获异常并 ``logger.error``，失败不会挡住后续钩子和串口关闭。
        """
        if self._closed:
            return
        self._closed = True
        while self._stops:
            name, callback = self._stops.pop()
            label = name or getattr(callback, "__name__", repr(callback))
            try:
                callback()
            except Exception:
                logger.error("紧急复位钩子执行失败 [%s]", label, exc_info=True)
        try:
            self._stack.close()
        except Exception:
            logger.error("释放硬件资源失败", exc_info=True)

    def __enter__(self) -> HardwareContext:
        """进入硬件上下文，返回自身。"""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """离开 ``with`` 时执行 :meth:`close_safely`，不吞掉原异常。

        :param exc_type: 块内异常类型；没有异常时为 None。
        :param exc: 异常实例。
        :param tb: 追溯对象。
        """
        self.close_safely()
