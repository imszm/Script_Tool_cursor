"""PC 工具 / 治具软件压力测试：由 YAML 描述的步骤序列驱动，窗口与继电器均可选。

迁移自 ``L5系列SMT&组装升级测试工具`` 下的两个脚本：

* ``PC_tool_工具软件压力测试-V1.2.py``：pywinauto 依次操作 PCTOOL 按钮（SN 写入、日志开关、
  IMU 校准、恢复出厂、参数设置弹窗确认、摇杆校准），窗口丢失 / 重连失败判定闪退并终止；
* ``治具工具软件压力测试（带继电器版）-V1.3.py``：ICSE 继电器模拟回弹按钮，左右转向灯交替。

``stress.sequences`` 为若干步骤列表，第 i 轮使用 ``sequences[(i-1) % N]``（V1.3 左右交替即两个序列）。
步骤动作::

    set_text / click / confirm_dialog   需要配置 window
    relay_on / relay_off / relay_all_off 需要配置 relay + serial.relay
    wait                                 仅等待

每步执行后等待 ``wait_s``。单轮任何异常计失败；配置了窗口时失败后尝试重连，重连失败即判定闪退并熔断。
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ppx_testkit.apps.pctool.common import (
    ControlRef,
    PcToolWindow,
    ToolWindow,
    WindowConfig,
    capture_window,
    is_failsafe,
    write_reports,
)
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.exceptions import ConfigError, GuiError, HardwareError, TestAbort
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)

WINDOW_ACTIONS = frozenset({"set_text", "click", "confirm_dialog"})
RELAY_ACTIONS = frozenset({"relay_on", "relay_off", "relay_all_off"})


class Relay(Protocol):
    def on(self, channel: int = 1) -> None: ...

    def off(self, channel: int = 1) -> None: ...

    def all_off(self) -> None: ...


# ============================================================ 配置
@dataclass(frozen=True)
class SnRule:
    mode: Literal["random", "sequence", "fixed"] = "random"
    prefix: str = ""
    random_min: int = 100
    random_max: int = 999
    start: int = 1
    width: int = 3
    value: str = ""

    def __post_init__(self) -> None:
        if self.mode == "random" and self.random_min > self.random_max:
            raise ConfigError(f"stress.sn.random_min({self.random_min}) 不能大于 random_max({self.random_max})")
        if self.mode == "sequence" and (self.width <= 0 or self.start < 0):
            raise ConfigError("stress.sn.width 必须 > 0 且 start >= 0")
        if self.mode == "fixed" and not self.value:
            raise ConfigError("stress.sn.mode=fixed 时必须配置 value")


@dataclass(frozen=True)
class StepConfig:
    action: Literal["set_text", "click", "confirm_dialog", "relay_on", "relay_off", "relay_all_off", "wait"]
    label: str = ""
    control: ControlRef | None = None
    invoke: bool = False
    text: str = "{sn}"
    channels: list[int] = field(default_factory=list)
    dialog_title_re: str = ""
    dialog_button: str = ""
    dialog_timeout_s: float = 8.0
    required: bool = True
    wait_s: float = 0.0

    def __post_init__(self) -> None:
        if self.wait_s < 0:
            raise ConfigError(f"步骤 {self.label or self.action} 的 wait_s 不能为负数")
        if self.action in ("set_text", "click") and self.control is None:
            raise ConfigError(f"步骤 {self.label or self.action} 缺少 control")
        if self.action == "confirm_dialog" and not (self.dialog_title_re and self.dialog_button):
            raise ConfigError(f"步骤 {self.label or self.action} 需要 dialog_title_re 与 dialog_button")
        if self.action in ("relay_on", "relay_off") and not self.channels:
            raise ConfigError(f"步骤 {self.label or self.action} 需要 channels")

    @property
    def name(self) -> str:
        return self.label or self.action


@dataclass(frozen=True)
class StepStressConfig:
    sequences: list[list[StepConfig]]
    cycles: int = 100000
    sequence_names: list[str] = field(default_factory=list)
    init_steps: list[StepConfig] = field(default_factory=list)
    final_steps: list[StepConfig] = field(default_factory=list)
    sn: SnRule = field(default_factory=SnRule)
    check_window_each_cycle: bool = True
    reconnect_on_error: bool = True
    stop_on_fail: bool = False
    screenshot_on_fail: bool = True

    def __post_init__(self) -> None:
        if self.cycles <= 0:
            raise ConfigError("stress.cycles 必须 > 0")
        if not self.sequences or any(not seq for seq in self.sequences):
            raise ConfigError("stress.sequences 至少包含一个非空步骤序列")
        if self.sequence_names and len(self.sequence_names) != len(self.sequences):
            raise ConfigError("stress.sequence_names 数量必须与 sequences 一致")

    def all_steps(self) -> list[StepConfig]:
        return [s for seq in self.sequences for s in seq] + list(self.init_steps) + list(self.final_steps)

    def validate_resources(self, *, has_window: bool, has_relay: bool) -> None:
        actions = {s.action for s in self.all_steps()}
        if actions & WINDOW_ACTIONS and not has_window:
            raise ConfigError(f"步骤包含窗口操作 {sorted(actions & WINDOW_ACTIONS)}，但未配置 window 段")
        if actions & RELAY_ACTIONS and not has_relay:
            raise ConfigError(f"步骤包含继电器操作 {sorted(actions & RELAY_ACTIONS)}，但未配置 relay 段")


# ============================================================ 纯逻辑
def make_sn(rule: SnRule, index: int, rng: random.Random) -> str:
    if rule.mode == "fixed":
        return rule.value
    if rule.mode == "sequence":
        return f"{rule.prefix}{rule.start + index - 1:0{rule.width}d}"
    return f"{rule.prefix}{rng.randint(rule.random_min, rule.random_max)}"


def next_sequence(current: int, count: int, passed: bool) -> int:
    """本轮成功才切换到下一个序列；失败时下一轮重复当前序列（与 V1.3 左右灯切换逻辑一致）。"""
    return (current + 1) % count if passed else current


def render_text(template: str, *, sn: str, index: int) -> str:
    try:
        return template.format(sn=sn, index=index)
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(f"文本模板 {template!r} 非法（可用占位符 {{sn}} {{index}}）: {exc}") from exc


# ============================================================ 流程
class StepStressCycle:
    def __init__(self, cfg: StepStressConfig, *, window: ToolWindow | None = None, relay: Relay | None = None,
                 transport: Any = None, ctx: RunContext | None = None,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None) -> None:
        cfg.validate_resources(has_window=window is not None, has_relay=relay is not None)
        self.cfg = cfg
        self.window = window
        self.relay = relay
        self.transport = transport
        self.ctx = ctx
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.rows: list[dict[str, Any]] = []
        self.last_action = ""
        self.seq_idx = 0

    # ------------------------------------------------------------ StressCycle
    def setup(self) -> None:
        if self.window is not None:
            self.window.connect()
        if self.cfg.init_steps:
            log.info("[系统初始化] 执行初始化步骤")
            for step in self.cfg.init_steps:
                self.execute_step(step, sn="", index=0)

    def run_cycle(self, index: int) -> CycleResult:
        seq_idx = self.seq_idx
        seq_name = self.cfg.sequence_names[seq_idx] if self.cfg.sequence_names else f"序列{seq_idx + 1}"
        sn = make_sn(self.cfg.sn, index, self._rng)
        log.info("===== 循环测试 %d 开始（%s）=====", index, seq_name)
        try:
            if self.window is not None and self.cfg.check_window_each_cycle and not self.window.is_alive():
                raise GuiError("检测到 PCTOOL 窗口丢失，可能已闪退。")
            for step in self.cfg.sequences[seq_idx]:
                self.execute_step(step, sn=sn, index=index)
        except TestAbort:
            raise
        except Exception as exc:  # noqa: BLE001 - 与旧脚本一致：任何异常计失败
            if is_failsafe(exc):
                raise
            detail = f"{exc}（最后动作: {self.last_action}）"
            log.error("第 %d 次循环异常: %s", index, detail, exc_info=True)
            if self.window is not None and self.cfg.screenshot_on_fail:
                capture_window(self.window, self.ctx, f"fail_cycle{index:06d}.png")
            self._record(index, seq_name, sn, False, detail)
            self.seq_idx = next_sequence(seq_idx, len(self.cfg.sequences), False)
            self._after_failure(index)
            return CycleResult(index, False, detail, data={"sn": sn, "last_action": self.last_action})

        self._record(index, seq_name, sn, True, "")
        self.seq_idx = next_sequence(seq_idx, len(self.cfg.sequences), True)
        return CycleResult(index, True, data={"sn": sn, "sequence": seq_name})

    def teardown(self, summary: RunSummary) -> None:
        summary.extra["最后动作"] = self.last_action
        try:
            if self.cfg.final_steps and not summary.keep_power:
                log.info("[结束] 执行收尾步骤")
                for step in self.cfg.final_steps:
                    try:
                        self.execute_step(step, sn="", index=0)
                    except (HardwareError, GuiError) as exc:
                        log.error("收尾步骤 %s 失败: %s", step.name, exc)
        finally:
            if self.transport is not None:
                self.transport.close()

    # ------------------------------------------------------------ 步骤执行
    def execute_step(self, step: StepConfig, *, sn: str, index: int) -> None:
        self.last_action = step.name
        action = step.action
        if action == "set_text":
            text = render_text(step.text, sn=sn, index=index)
            self.last_action = f"{step.name}: {text}"
            self._require_window().set_text(self._require_control(step), text)
        elif action == "click":
            self._require_window().click(self._require_control(step), invoke=step.invoke)
        elif action == "confirm_dialog":
            ok = self._require_window().confirm_dialog(step.dialog_title_re, step.dialog_button,
                                                       step.dialog_timeout_s)
            if not ok:
                msg = f"未检测到弹窗 {step.dialog_title_re} 或按钮 '{step.dialog_button}'"
                if step.required:
                    raise GuiError(msg)
                log.warning(msg)
        elif action in ("relay_on", "relay_off"):
            relay = self._require_relay()
            for ch in step.channels:
                (relay.on if action == "relay_on" else relay.off)(ch)
        elif action == "relay_all_off":
            self._require_relay().all_off()
        log.info("%s", self.last_action)
        if step.wait_s > 0:
            log.debug("等待 %.2f 秒 -> %s", step.wait_s, step.name)
            self._sleep(step.wait_s)

    def _require_window(self) -> ToolWindow:
        if self.window is None:
            raise ConfigError("步骤需要窗口，但未配置 window 段")
        return self.window

    def _require_relay(self) -> Relay:
        if self.relay is None:
            raise ConfigError("步骤需要继电器，但未配置 relay 段")
        return self.relay

    @staticmethod
    def _require_control(step: StepConfig) -> ControlRef:
        if step.control is None:
            raise ConfigError(f"步骤 {step.name} 缺少 control")
        return step.control

    def _after_failure(self, index: int) -> None:
        if self.window is None or not self.cfg.reconnect_on_error:
            return
        try:
            self.window.connect()
            log.info("第 %d 次循环失败后已重新连接窗口", index)
        except GuiError as exc:
            raise TestAbort(f"检测到 PCTOOL 闪退（重连失败: {exc}），最后执行动作: {self.last_action}") from exc

    def _record(self, index: int, seq: str, sn: str, passed: bool, detail: str) -> None:
        self.rows.append({"cycle": index, "sequence": seq, "sn": sn, "verdict": "PASS" if passed else "FAIL",
                          "detail": detail})


def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("stress", StepStressConfig)
    has_window, has_relay = settings.has("window"), settings.has("relay")
    cfg.validate_resources(has_window=has_window, has_relay=has_relay)

    window = PcToolWindow(settings.section("window", WindowConfig)) if has_window else None
    transport = relay = None
    if has_relay:
        from ppx_testkit.core.factory import open_serial, relay_from_settings

        transport = open_serial(settings, "relay")
    try:
        if transport is not None:
            relay = relay_from_settings(settings, transport)
        cycle = StepStressCycle(cfg, window=window, relay=relay, transport=transport, ctx=ctx)
        summary = StressRunner(cycle, cfg.cycles, station=settings.station, stop_on_fail=cfg.stop_on_fail).run()
    finally:
        if transport is not None:
            transport.close()
    write_reports(ctx, "report", title=f"{settings.station} 压力测试报告", rows=cycle.rows,
                  summary=summary.to_dict(), columns=["cycle", "sequence", "sn", "verdict", "detail"])
    return 0 if summary.ok else 1
