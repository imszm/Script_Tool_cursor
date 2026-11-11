"""R3 MCB 白盒测试器：mixin 方法绑定、全自动 / 压力 / 菜单流程与异常停机（不加载 DLL，不开串口）。"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ppx_testkit.apps.whitebox.r3_mcb import (
    BasicTests,
    R3McbConfig,
    R3McbTester,
    RampProfile,
    StabilityTests,
)
from ppx_testkit.core.protocol.ppx_types import ModeV2, RegionDataV2, RegV2
from ppx_testkit.exceptions import HardwareError, SerialDisconnectedError


class _Transport:
    """记录直接写入的噪声包，并提供 reconnect。"""

    def __init__(self) -> None:
        self.is_open = True
        self.raw: list[bytes] = []
        self.reconnects = 0

    def write(self, data: bytes) -> int:
        """记下原始字节。

        :param data: 噪声包。
        :return: 写入字节数。
        """
        self.raw.append(bytes(data))
        return len(data)

    def reconnect(self, delay_s: float = 0.0) -> None:
        """模拟重连成功。

        :param delay_s: 重连前等待秒数，这里不使用。
        """
        self.reconnects += 1


class _NoReconnectTransport:
    """不提供 reconnect 的串口，断开后测试器只能熔断。"""

    is_open = True

    def write(self, data: bytes) -> int:
        """丢弃原始字节。

        :param data: 噪声包。
        :return: 写入字节数。
        """
        return len(data)


class _R3Client:
    """按寄存器读写一份 RegionDataV2 的假 MCB。骑行模式下实际转速跟随目标转速。"""

    def __init__(
        self,
        transport: Any = None,
        *,
        fail_regs: Iterable[int] = (),
        read_error: Callable[[], Exception] = lambda: HardwareError("读寄存器失败"),
    ) -> None:
        self.data = RegionDataV2()
        self.data.bus_voltage = 360
        self.transport = transport if transport is not None else _Transport()
        self.fail_regs = set(fail_regs)
        self.read_error = read_error
        self.writes: list[tuple[int, dict[str, Any]]] = []

    def read(self, reg: int, nums: int = 1, *, label: str = "") -> RegionDataV2:
        """读一段寄存器。命中 ``fail_regs`` 时抛出 ``read_error`` 构造的异常。

        :param reg: 起始寄存器。
        :param nums: 寄存器个数，这里不使用。
        :param label: 日志标签，这里不使用。
        :return: 当前数据的副本。
        """
        if reg in self.fail_regs:
            raise self.read_error()
        running = self.data.run_mode == ModeV2.RUNNING
        self.data.motor_speed = self.data.target_speed if running else 0
        return RegionDataV2.from_buffer_copy(self.data)

    def write(
        self,
        reg: int,
        fields: Mapping[str, Any],
        *,
        nums: int = 1,
        expect_response: bool = True,
        label: str = "",
    ) -> None:
        """记录写入并更新字段。

        :param reg: 起始寄存器。
        :param fields: 字段名到值。
        :param nums: 寄存器个数，这里不使用。
        :param expect_response: 是否等应答，这里不使用。
        :param label: 日志标签，这里不使用。
        """
        self.writes.append((reg, dict(fields)))
        for name, value in fields.items():
            setattr(self.data, name, value)


class _Clock:
    """每读一次前进 10ms，避免按时长循环的用例死等。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        """返回递增的单调时间。

        :return: 秒。
        """
        self.t += 0.01
        return self.t


def _cfg(**overrides: Any) -> R3McbConfig:
    """轮数和等待都压到最小的配置。

    :param overrides: 覆盖 R3McbConfig 的字段。
    :return: 配置对象。
    """
    fast_ramp = RampProfile(max_speed=100, step=100, dwell_s=0)
    values: dict[str, Any] = {
        "delay_scale": 0.0,
        "ramp": fast_ramp,
        "basic": BasicTests(speed_points=[100], current_points=[50], ramp=fast_ramp, rapid_count=1,
                            rapid_interval_s=0),
        "stability": StabilityTests(
            long_run_cycles=2, heartbeat_interval_s=0, burst_count=3, burst_interval_s=0, idle_wait_s=0,
            mode_switch_rounds=1, ramp_cycles=1, ramp=fast_ramp, jump_pairs=[(0, 100)], jump_repeat=1,
            jump_interval_s=0, noise_packets=[b"\x00"], noise_settle_s=0, noise_interval_s=0,
            monitor_duration_s=0.05,
        ),
    }
    values.update(overrides)
    return R3McbConfig(**values)


def _tester(client: _R3Client, cfg: R3McbConfig, inputs: Iterable[str] = ()) -> R3McbTester:
    """组装测试器。菜单输入用完后抛 EOFError，与关闭标准输入时一致。

    :param client: 假 MCB。
    :param cfg: 配置。
    :param inputs: 菜单依次读到的输入。
    :return: 测试器。
    """
    feed = iter(inputs)

    def _input(_prompt: str) -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    return R3McbTester(client, cfg, sleep=lambda _s: None, clock=_Clock(), input_fn=_input)


def test_mixin_methods_bind_correctly() -> None:
    problems: list[str] = []
    for cls in R3McbTester.__mro__[:-1]:
        for name, obj in vars(cls).items():
            if isinstance(obj, staticmethod):
                params = list(inspect.signature(obj.__func__).parameters)
                if params and params[0] == "self":
                    problems.append(f"{cls.__name__}.{name} 是 staticmethod 却声明了 self")
            elif inspect.isfunction(obj):
                params = list(inspect.signature(obj).parameters)
                if not params or params[0] != "self":
                    problems.append(f"{cls.__name__}.{name} 缺少 self")
    assert not problems, problems


def test_crash_suffix_on_class_and_instance() -> None:
    tester = _tester(_R3Client(), _cfg())
    for owner in (R3McbTester, tester):
        assert owner._crash_suffix(True) == " | 死机"
        assert owner._crash_suffix(True, " | 检测到死机") == " | 检测到死机"
        assert owner._crash_suffix(False, " | x") == ""


def test_full_action_runs_all_stages() -> None:
    client = _R3Client()
    tester = _tester(client, _cfg(action="full"))
    assert tester.execute() in (0, 1)
    names = {r["name"] for r in tester.results}
    assert {"基础连接验证", "长时间运行稳定性", "通信噪声容限", "持续心跳监控", "最终安全停止"} <= names
    assert all(isinstance(r["details"], str) for r in tester.results)
    assert client.transport.raw == [b"\x00"]


def test_stress_long_run_reports_crash() -> None:
    client = _R3Client(fail_regs={RegV2.MCU_ERRCODE})
    stability = StabilityTests(cases=["long_run"], crash_threshold=1, long_run_cycles=2, heartbeat_interval_s=0)
    tester = _tester(client, _cfg(action="stress", stability=stability))
    assert tester.execute() == 1
    row = next(r for r in tester.results if r["name"] == "长时间运行稳定性")
    assert row["verdict"] == "FAIL"
    assert "检测到死机" in row["details"]


def test_interactive_menu_runs_full_then_quits() -> None:
    tester = _tester(_R3Client(), _cfg(action="interactive"), inputs=["a", "q"])
    assert tester.execute() == 0
    assert any(r["name"] == "最终安全停止" for r in tester.results)


def test_disconnect_without_reconnect_aborts_and_stops_motor() -> None:
    client = _R3Client(
        _NoReconnectTransport(),
        fail_regs={RegV2.MCU_ERRCODE},
        read_error=lambda: SerialDisconnectedError("FAKE", "gone"),
    )
    tester = _tester(client, _cfg(action="status"))
    assert tester.execute() == 1
    assert any(r["name"] == "测试熔断" for r in tester.results)
    stop = {"target_speed": 0, "target_accel": tester.cfg.target_accel, "target_current": 0}
    assert (RegV2.TARGET_SPEED, stop) in client.writes
    assert (RegV2.RUN_MODE, {"run_mode": int(ModeV2.IDLE)}) in client.writes
