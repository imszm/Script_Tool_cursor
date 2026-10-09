"""统一异常体系。

所有工具内部抛出的异常都继承自 :class:`ToolError`，上层（CLI / StressRunner）
据此区分“可预期的硬件/配置故障”和“程序缺陷”。
"""

from __future__ import annotations


class ToolError(Exception):
    """工具集所有自定义异常的基类。"""


class ConfigError(ToolError):
    """配置缺失、类型错误或取值非法。"""


# ---------------------------------------------------------------- 硬件
class HardwareError(ToolError):
    pass


class SerialPortError(HardwareError):
    def __init__(self, port: str | None, message: str) -> None:
        self.port = port
        super().__init__(f"[{port or '未知端口'}] {message}")


class SerialOpenError(SerialPortError):
    """端口不存在、被占用或重试后仍无法打开。"""


class SerialTimeoutError(SerialPortError):
    """写超时，或在截止时间内没有收到期望的数据。"""


class SerialDisconnectedError(SerialPortError):
    """读写过程中端口失效（USB 拔出、驱动异常等）。"""


class PortNotFoundError(SerialOpenError):
    """按匹配规则找不到对应的串口。"""


class RelayError(HardwareError):
    pass


# ---------------------------------------------------------------- 协议
class ProtocolError(ToolError):
    pass


class DllLoadError(ProtocolError):
    pass


class FrameFormatError(ProtocolError):
    pass


class FrameParseError(ProtocolError):
    pass


class DeviceExceptionResponse(ProtocolError):
    """设备返回了异常应答帧（cmd & 0xC0 == 0xC0）。"""


# ---------------------------------------------------------------- GUI
class GuiError(ToolError):
    pass


class ElementNotFoundError(GuiError):
    pass


class WindowNotFoundError(GuiError):
    pass


# ---------------------------------------------------------------- 流程
class TestAbort(ToolError):
    """主动熔断测试。

    ``keep_power`` 为 True 表示“保留现场”：退出时不得给被测设备断电。
    """

    __test__ = False  # 避免被 pytest 当成测试类收集

    def __init__(self, reason: str, *, keep_power: bool = False) -> None:
        self.reason = reason
        self.keep_power = keep_power
        super().__init__(reason)
