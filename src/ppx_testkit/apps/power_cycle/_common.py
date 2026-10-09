"""开关机 / 充电 / 舵机压测应用共用的组装与收尾工具（仅供 apps 内部使用）。"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence

from ppx_testkit.core.factory import endpoint, keyword_evaluator, open_serial
from ppx_testkit.core.monitor.line_reader import DeviceLogMonitor
from ppx_testkit.core.runner.result import RunSummary
from ppx_testkit.core.serial.port_finder import PortFinder
from ppx_testkit.core.serial.transport import SerialFactory, SerialTransport
from ppx_testkit.exceptions import ConfigError, SerialPortError
from ppx_testkit.settings import AppSettings
from ppx_testkit.utils.notify import show_message

log = logging.getLogger(__name__)


def open_ports(
    settings: AppSettings,
    specs: Sequence[tuple[str, bool]],
    *,
    factory: SerialFactory | None = None,
    finder: PortFinder | None = None,
) -> dict[str, SerialTransport]:
    """按 ``(串口名, 是否立即打开)`` 依次定位并打开 ``serial.<名>``。

    后打开的端口会排除已占用的端口号，避免自动识别时两路匹配到同一个口；
    任一端口失败时关闭已打开的端口再抛出原异常。
    """
    opened: dict[str, SerialTransport] = {}
    try:
        for name, auto_open in specs:
            opened[name] = open_serial(
                settings,
                name,
                exclude=[t.port for t in opened.values()],
                finder=finder,
                factory=factory,
                auto_open=auto_open,
            )
    except BaseException:
        close_ports(*opened.values())
        raise
    return opened


def close_ports(*transports: SerialTransport | None) -> None:
    """关闭全部串口；SerialTransport.close 幂等且不抛异常，可放在 finally 中。"""
    for t in transports:
        if t is not None:
            t.close()


def build_monitor(
    settings: AppSettings,
    transport: SerialTransport,
    *,
    name: str = "device",
    no_data_warning_s: float | None = None,
) -> DeviceLogMonitor:
    """以 ``serial.<name>`` 的编码设置与 ``keywords`` 段构建设备日志监听器。"""
    ep = endpoint(settings, name)
    return DeviceLogMonitor(
        transport,
        keyword_evaluator(settings),
        encoding=ep.encoding,
        errors=ep.encoding_errors,
        raw_source=name,
        no_data_warning_s=no_data_warning_s,
    )


def try_reconnect(transport: SerialTransport, delay_s: float) -> bool:
    """设备串口断开后尝试重连（同一端口号）；成功返回 True，失败只记录日志。"""
    log.warning("设备串口 %s 断开，%.1fs 后尝试重连...", transport.port, delay_s)
    try:
        transport.reconnect(delay_s=delay_s)
    except SerialPortError as exc:
        log.error("设备串口重连失败: %s", exc)
        return False
    log.info("设备串口重连成功: %s", transport.port)
    return True


def pick_duration(base_s: float, max_s: float | None, rng: random.Random) -> float:
    """``max_s`` 为空时返回固定时长，否则在 [base_s, max_s] 内均匀随机（保留两位小数）。"""
    if max_s is None or max_s <= base_s:
        return base_s
    return round(rng.uniform(base_s, max_s), 2)


def require_non_negative(section: str, **values: float | None) -> None:
    for name, value in values.items():
        if value is not None and value < 0:
            raise ConfigError(f"{section}.{name} 不能为负数，实际 {value}")


def require_runner_options(section: str, cycles: int, max_consecutive_failures: int | None) -> None:
    if cycles <= 0:
        raise ConfigError(f"{section}.cycles 必须 > 0，实际 {cycles}")
    if max_consecutive_failures is not None and max_consecutive_failures < 1:
        raise ConfigError(f"{section}.max_consecutive_failures 至少为 1，实际 {max_consecutive_failures}")


def finish(summary: RunSummary, *, title: str, notify: bool) -> int:
    """根据运行结果弹窗提示操作员，并返回进程退出码（0 通过 / 1 失败）。"""
    if notify:
        if summary.aborted:
            keep = "\n设备保持当前供电状态以供现场排查" if summary.keep_power else ""
            show_message(f"测试熔断停止{keep}\n原因: {summary.abort_reason}", f"{title} - 熔断", error=True)
        elif summary.error:
            show_message(f"测试因故障终止\n{summary.error}", f"{title} - 异常", error=True)
        elif not summary.interrupted:
            show_message(
                f"测试结束\n通过 {summary.passed} / 失败 {summary.failed}（通过率 {summary.pass_rate:.2f}%）",
                f"{title} - 完成",
                error=summary.failed > 0,
            )
    return 0 if summary.ok else 1
