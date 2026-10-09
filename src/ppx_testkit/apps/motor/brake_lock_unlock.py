"""电磁刹车解锁 / 上锁成功率压测（R3 v2 region 协议）。

迁移自 ``R3电机专项测试/电磁刹车解锁和上锁测试_优化完整版.py``（基线），并吸收
``电磁刹车解锁和上锁测试.py`` 中的成功率统计、上锁回读校验与超温保护（可选）。

旧脚本用两个线程 + 本机 TCP 互发 unlock/lock 消息来协调被测、负载两台电机；
两台电机实际由同一进程控制，这里改为单线程按旧消息时序顺序执行，每轮流程::

    负载: [清错] -> 推行模式 -> 下发转速/电流 -> 预运行 pre_run_s
    被测: [读错误码, 非 0 则清错] -> 解锁 -> 轮询 (错误码==0 且 刹车==OPENED)，超时判失败
    被测: 骑行模式 -> 下发转速/电流 -> 双机对拖 run_time_s
    被测: 速度归零 -> 等待 stop_before_lock_s -> 上锁 -> [轮询 刹车==CLOSED]
    负载: 速度归零

异常策略：单条指令的通信/协议失败按旧脚本语义重试并判定本轮结果；串口断开时
重连一次，重连失败抛出 HardwareError 交由 StressRunner 终止测试。无论如何结束，
teardown 都会把两台电机速度归零、按配置给被测电机上锁，串口由 :func:`run` 在 finally 中关闭。
"""

from __future__ import annotations

import contextlib
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ppx_testkit.core.factory import open_serial
from ppx_testkit.core.protocol.dll_loader import PpxDll
from ppx_testkit.core.protocol.ppx_types import BrakeStateV2, DevId, ModeV2, RegV2, RtSetting
from ppx_testkit.core.protocol.region_client import RegionClient, RegionCodecV2
from ppx_testkit.core.report.writers import write_csv, write_html
from ppx_testkit.core.runner import CycleResult, RunSummary, StressRunner
from ppx_testkit.exceptions import (
    ConfigError,
    HardwareError,
    ProtocolError,
    SerialDisconnectedError,
    TestAbort,
    ToolError,
)
from ppx_testkit.logger import RunContext, get_logger
from ppx_testkit.settings import AppSettings

log = get_logger(__name__)

CLR_ERRCODE_BIT = int(RtSetting.CLR_ERRCODE)


# ============================================================ 配置
@dataclass(frozen=True)
class RegionProtocolConfig:
    dll_path: Path
    dev_id: int = DevId.MCB
    rx_timeout_s: float = 0.3
    retries: int = 3
    retry_delay_s: float = 0.03

    def __post_init__(self) -> None:
        if self.rx_timeout_s <= 0:
            raise ConfigError("protocol.rx_timeout_s 必须 > 0")
        if self.retries < 1:
            raise ConfigError("protocol.retries 至少为 1")
        if self.retry_delay_s < 0:
            raise ConfigError("protocol.retry_delay_s 不能为负数")


@dataclass(frozen=True)
class MotorProfile:
    run_mode: int
    speed: int
    accel: int = 100
    current: int = 0


@dataclass(frozen=True)
class OverheatConfig:
    """超温保护（来自原版脚本，默认关闭）。超过 limit 立即停机，降到 recover 以下才继续。"""

    enabled: bool = False
    mos_limit: float = 85.0
    motor_limit: float = 150.0
    mos_recover: float = 70.0
    motor_recover: float = 120.0
    check_interval_s: float = 0.2
    cooldown_poll_s: float = 2.0
    max_read_failures: int = 10
    cooldown_timeout_s: float = 0.0  # 0 表示不限时

    def __post_init__(self) -> None:
        if self.mos_recover >= self.mos_limit or self.motor_recover >= self.motor_limit:
            raise ConfigError("overheat: 恢复阈值必须低于超温阈值（回差）")
        if self.check_interval_s <= 0 or self.cooldown_poll_s <= 0:
            raise ConfigError("overheat: check_interval_s / cooldown_poll_s 必须 > 0")
        if self.max_read_failures < 1:
            raise ConfigError("overheat.max_read_failures 至少为 1")
        if self.cooldown_timeout_s < 0:
            raise ConfigError("overheat.cooldown_timeout_s 不能为负数")


@dataclass(frozen=True)
class BrakeTestConfig:
    cycles: int = 1000
    round_interval_s: float = 3.0
    stop_on_fail: bool = False
    max_consecutive_failures: int = 0  # 0 表示不按连续失败熔断
    clear_error_on_start: bool = True
    clear_error_each_round: bool = True  # 负载电机每轮清错
    clear_error_before_unlock: bool = True  # 被测电机解锁前读错误码，非 0 则清除
    command_retries: int = 3
    command_retry_delay_s: float = 0.1
    #: brake_reg: 写 brake_state=OPENED/CLOSED（优化版）；clutch_mode/lock_mode: 写 run_mode=12/3（原版）
    unlock_method: Literal["brake_reg", "clutch_mode"] = "brake_reg"
    lock_method: Literal["brake_reg", "lock_mode"] = "brake_reg"
    unlock_check_timeout_s: float = 1.0
    unlock_poll_interval_s: float = 0.1
    relock_on_unlock_fail: bool = True
    verify_lock: bool = True
    lock_check_timeout_s: float = 1.0
    lock_poll_interval_s: float = 0.1
    pre_run_s: float = 1.0
    run_time_min_s: float = 1.0
    run_time_max_s: float = 1.0
    stop_before_lock_s: float = 1.0
    stop_accel: int = 100
    keepalive_interval_s: float = 0.0  # >0 时在运行阶段周期重发转速/电流
    hold_slice_s: float = 0.02
    lock_on_teardown: bool = True
    reconnect_delay_s: float = 0.5
    tested: MotorProfile = field(default_factory=lambda: MotorProfile(ModeV2.RUNNING, -1000, 100, 10))
    load: MotorProfile = field(default_factory=lambda: MotorProfile(ModeV2.PWR_PUSH, 1000, 100, 20))
    overheat: OverheatConfig = field(default_factory=OverheatConfig)

    def __post_init__(self) -> None:
        if self.cycles <= 0:
            raise ConfigError("brake_test.cycles 必须 > 0")
        if self.command_retries < 1:
            raise ConfigError("brake_test.command_retries 至少为 1")
        if self.run_time_max_s < self.run_time_min_s:
            raise ConfigError("brake_test.run_time_max_s 不能小于 run_time_min_s")
        if self.unlock_check_timeout_s <= 0 or self.lock_check_timeout_s <= 0:
            raise ConfigError("brake_test 解锁/上锁检测超时必须 > 0")
        if self.hold_slice_s <= 0:
            raise ConfigError("brake_test.hold_slice_s 必须 > 0")
        negatives = [n for n in ("round_interval_s", "command_retry_delay_s", "unlock_poll_interval_s",
                                 "lock_poll_interval_s", "pre_run_s", "run_time_min_s", "stop_before_lock_s",
                                 "keepalive_interval_s", "reconnect_delay_s") if getattr(self, n) < 0]
        if negatives:
            raise ConfigError(f"brake_test 以下时间参数不能为负数: {negatives}")


# ============================================================ 单台电机链路
class RegionClientLike(Protocol):
    transport: Any

    def read(self, reg: int, nums: int = 1, *, label: str = "") -> Any: ...

    def write(self, reg: int, fields: Mapping[str, Any], *, nums: int = 1, expect_response: bool = True,
              label: str = "") -> Any: ...


class MotorLink:
    """对 RegionClient 的业务封装：通信失败返回 False/None，串口断开时重连一次。"""

    def __init__(
        self,
        client: RegionClientLike,
        label: str,
        *,
        retries: int = 3,
        retry_delay_s: float = 0.1,
        reconnect_delay_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.label = label
        self.retries = max(1, retries)
        self.retry_delay_s = retry_delay_s
        self.reconnect_delay_s = reconnect_delay_s
        self._sleep = sleep

    def _reconnect(self, exc: SerialDisconnectedError) -> None:
        transport = getattr(self.client, "transport", None)
        if transport is None or not hasattr(transport, "reconnect"):
            raise exc
        log.warning("%s串口断开，尝试重连: %s", self.label, exc)
        transport.reconnect(delay_s=self.reconnect_delay_s)  # 失败抛出 SerialOpenError，由上层终止测试
        log.info("%s串口重连成功", self.label)

    def _try(self, desc: str, fn: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, fn()
        except SerialDisconnectedError as exc:
            self._reconnect(exc)
        except (HardwareError, ProtocolError) as exc:
            log.warning("%s %s 失败: %s", self.label, desc, exc)
        return False, None

    def command(self, desc: str, fn: Callable[[], Any]) -> bool:
        for i in range(1, self.retries + 1):
            ok, _ = self._try(desc, fn)
            if ok:
                log.info("%s %s 成功", self.label, desc)
                return True
            log.warning("%s %s 失败，重试 %d/%d", self.label, desc, i, self.retries)
            if i < self.retries and self.retry_delay_s > 0:
                self._sleep(self.retry_delay_s)
        log.error("%s %s 重试 %d 次后仍失败", self.label, desc, self.retries)
        return False

    # ------------------------------------------------------------ 指令
    def clear_error(self) -> bool:
        ok, _ = self._try("清除错误码", lambda: self.client.write(
            RegV2.RT_SETTING, {"rt_setting": CLR_ERRCODE_BIT}, label=f"{self.label}清错"))
        return ok

    def set_run_mode(self, mode: int) -> bool:
        return self.command(f"切换模式{mode}", lambda: self.client.write(
            RegV2.RUN_MODE, {"run_mode": mode}, label=f"{self.label}模式{mode}"))

    def set_speed(self, speed: int, accel: int, current: int, *, retry: bool = True) -> bool:
        desc = f"设置转速{speed}/加速度{accel}/电流{current}"

        def _do() -> Any:
            return self.client.write(RegV2.TARGET_SPEED, {
                "target_speed": speed, "target_accel": accel, "target_current": current,
            }, nums=3, label=f"{self.label}转速{speed}")

        if retry:
            return self.command(desc, _do)
        return self._try(desc, _do)[0]

    def write_brake(self, state: int) -> bool:
        return self.command(f"写刹车状态{state}", lambda: self.client.write(
            RegV2.BRAKE_STATE, {"brake_state": state}, label=f"{self.label}刹车{state}"))

    # ------------------------------------------------------------ 读取
    def read_error_code(self) -> int | None:
        ok, data = self._try("读错误码", lambda: self.client.read(RegV2.MCU_ERRCODE, 1, label=f"{self.label}错误码"))
        return int(data.mcu_errcode) if ok else None

    def read_brake_state(self) -> int | None:
        ok, data = self._try("读刹车状态", lambda: self.client.read(RegV2.BRAKE_STATE, 1, label=f"{self.label}刹车"))
        return int(data.brake_state) if ok else None

    def read_temperatures(self) -> tuple[int, int] | None:
        ok, data = self._try("读温度", lambda: self.client.read(RegV2.MOSFET_TEMP, 2, label=f"{self.label}温度"))
        return (int(data.mosfet_temp), int(data.motor_temp)) if ok else None


class _OverheatInterrupt(Exception):
    """运行阶段检测到超温（内部控制流，仅在本模块内捕获）。"""


def _fmt_err(err: int | None) -> str:
    return f"0x{err:X}" if err is not None else "读取失败"


# ============================================================ 压测循环
class BrakeLockUnlockCycle:
    def __init__(
        self,
        tested: MotorLink,
        load: MotorLink,
        cfg: BrakeTestConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.tested = tested
        self.load = load
        self.cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self.rows: list[dict[str, Any]] = []
        self.unlock_attempts = 0
        self.unlock_success = 0
        self.lock_attempts = 0
        self.lock_success = 0
        self.overheat_interrupts = 0
        self.cooldowns = 0
        self.last_temps: dict[str, tuple[int, int]] = {}
        self._overheat_pending = False

    # ------------------------------------------------------------ StressCycle
    def setup(self) -> None:
        if self.cfg.clear_error_on_start:
            for link in (self.tested, self.load):
                link.clear_error()
                log.info(">>> %s启动时清除一次错误码", link.label)

    def run_cycle(self, index: int) -> CycleResult:
        row: dict[str, Any] = {"index": index}
        if self.cfg.overheat.enabled:
            self._precheck_temperature()
        try:
            passed, detail = self._round(row)
        except _OverheatInterrupt as exc:
            self.overheat_interrupts += 1
            self._overheat_pending = True
            log.error("超温！%s，立即停止双机并上锁", exc)
            self._stop_all(lock=True)
            passed, detail = False, f"超温中断: {exc}"
            row["overheat"] = True
        row["verdict"] = "PASS" if passed else "FAIL"
        row["detail"] = detail
        row.update({f"{k}_temp": f"{v[0]}/{v[1]}" for k, v in self.last_temps.items()})
        self.rows.append(row)
        self._log_rates(index)
        return CycleResult(index=index, passed=passed, detail=detail, data=row)

    def teardown(self, summary: RunSummary) -> None:
        log.info(">>> 收尾：双机速度归零%s", "，被测电机上锁" if self.cfg.lock_on_teardown else "")
        self._stop_all(lock=self.cfg.lock_on_teardown)
        summary.extra.update(self.stats())

    # ------------------------------------------------------------ 统计
    @staticmethod
    def _rate(ok: int, total: int) -> str:
        return f"{ok}/{total} ({ok / total * 100:.1f}%)" if total else "0/0"

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "解锁成功率": self._rate(self.unlock_success, self.unlock_attempts),
            "上锁成功率": self._rate(self.lock_success, self.lock_attempts),
        }
        if self.cfg.overheat.enabled:
            out["超温中断轮数"] = self.overheat_interrupts
            out["冷却等待次数"] = self.cooldowns
        return out

    def _log_rates(self, index: int) -> None:
        log.info(">>> 第 %d 轮完成 | 解锁: %s | 上锁: %s", index,
                 self._rate(self.unlock_success, self.unlock_attempts),
                 self._rate(self.lock_success, self.lock_attempts))

    # ------------------------------------------------------------ 单轮流程
    def _round(self, row: dict[str, Any]) -> tuple[bool, str]:
        cfg = self.cfg
        t, ld = self.tested, self.load

        if cfg.clear_error_each_round:
            ld.clear_error()
        log.info(">>> 负载切换到推行模式")
        load_ok = ld.set_run_mode(cfg.load.run_mode)
        log.info(">>> 负载启动运行（速度%d，电流%d）", cfg.load.speed, cfg.load.current)
        load_ok = ld.set_speed(cfg.load.speed, cfg.load.accel, cfg.load.current) and load_ok
        row["load_started"] = load_ok
        if not load_ok:
            log.warning("负载电机启动指令未全部成功，继续本轮")
        log.info(">>> 预运行 %.1fs", cfg.pre_run_s)
        self._hold(cfg.pre_run_s, [(ld, cfg.load)])

        self.unlock_attempts += 1
        unlock_ok, unlock_detail = self._unlock(row)
        row["unlock_ok"] = unlock_ok
        if not unlock_ok:
            log.error("解锁失败：%s，停止当前轮次", unlock_detail)
            ld.set_speed(0, cfg.stop_accel, 0)
            t.set_speed(0, cfg.stop_accel, 0)
            if cfg.relock_on_unlock_fail:
                self._lock_command()
            return False, f"解锁失败: {unlock_detail}"
        self.unlock_success += 1

        log.info(">>> 被测切换到骑行模式，目标转速 %d", cfg.tested.speed)
        t.set_run_mode(cfg.tested.run_mode)
        t.set_speed(cfg.tested.speed, cfg.tested.accel, cfg.tested.current)
        run_time = cfg.run_time_min_s
        if cfg.run_time_max_s > cfg.run_time_min_s:
            run_time = self._rng.uniform(cfg.run_time_min_s, cfg.run_time_max_s)
        row["run_time_s"] = round(run_time, 2)
        log.info(">>> 对拖运行 %.1fs", run_time)
        self._hold(run_time, [(ld, cfg.load), (t, cfg.tested)])

        lock_ok, lock_detail = self._lock(row)
        row["lock_ok"] = lock_ok
        log.info(">>> 负载停止")
        ld.set_speed(0, cfg.stop_accel, 0)
        if lock_ok:
            self.lock_success += 1
            return True, f"{unlock_detail}; {lock_detail}"
        return False, f"上锁失败: {lock_detail}"

    def _unlock(self, row: dict[str, Any]) -> tuple[bool, str]:
        cfg = self.cfg
        t = self.tested
        if cfg.clear_error_before_unlock:
            err = t.read_error_code()
            if err:
                log.warning("检测到历史错误码 0x%X，执行自动清除以防止解锁失败", err)
                t.clear_error()
                self._sleep(0.1)
        log.info(">>> 执行解锁（%s）", "写刹车寄存器" if cfg.unlock_method == "brake_reg" else "离合打开模式")
        if cfg.unlock_method == "brake_reg":
            cmd_ok = t.write_brake(BrakeStateV2.OPENED)
        else:
            cmd_ok = t.set_run_mode(ModeV2.CLUTCH_OPEN)
        if not cmd_ok:
            log.warning("解锁指令未成功，仍按超时轮询状态")

        start = self._clock()
        err: int | None = None
        brk: int | None = None
        while self._clock() - start < cfg.unlock_check_timeout_s:
            err = t.read_error_code()
            brk = t.read_brake_state()
            log.info("解锁轮询: 错误码=%s, 刹车状态=%s", _fmt_err(err), brk)
            if err == 0 and brk == BrakeStateV2.OPENED:
                elapsed_ms = (self._clock() - start) * 1000
                row["unlock_ms"] = round(elapsed_ms, 1)
                log.info("解锁成功（错误码=0，刹车已打开）")
                return True, f"解锁成功({elapsed_ms:.0f}ms)"
            self._sleep(cfg.unlock_poll_interval_s)
        row["unlock_err"] = _fmt_err(err)
        row["unlock_brake"] = brk
        return False, f"错误码={_fmt_err(err)}, 刹车状态={brk}" + ("" if cmd_ok else ", 解锁指令失败")

    def _lock_command(self) -> bool:
        if self.cfg.lock_method == "brake_reg":
            return self.tested.write_brake(BrakeStateV2.CLOSED)
        return self.tested.set_run_mode(ModeV2.LOCK)

    def _lock(self, row: dict[str, Any]) -> tuple[bool, str]:
        cfg = self.cfg
        t = self.tested
        log.info(">>> 被测减速停止")
        t.set_speed(0, cfg.stop_accel, 0)
        self._hold(cfg.stop_before_lock_s, [(self.load, cfg.load)])
        self.lock_attempts += 1
        log.info(">>> 执行上锁（%s）", "写刹车寄存器" if cfg.lock_method == "brake_reg" else "锁车模式")
        if not self._lock_command():
            return False, "上锁指令失败"
        if not cfg.verify_lock:
            return True, "上锁指令已发送(未回读校验)"
        start = self._clock()
        brk: int | None = None
        while self._clock() - start < cfg.lock_check_timeout_s:
            brk = t.read_brake_state()
            if brk == BrakeStateV2.CLOSED:
                log.info("刹车已闭合，上锁成功")
                return True, "上锁成功"
            self._sleep(cfg.lock_poll_interval_s)
        row["lock_brake"] = brk
        log.error("刹车状态异常: %s，上锁失败", brk)
        return False, f"刹车状态={brk}"

    # ------------------------------------------------------------ 运行保持 / 超温
    def _hold(self, seconds: float, running: list[tuple[MotorLink, MotorProfile]]) -> None:
        """等待 seconds 秒；按配置周期重发转速、检测超温（超温抛 _OverheatInterrupt）。"""
        if seconds <= 0:
            return
        cfg = self.cfg
        ka = cfg.keepalive_interval_s
        if not cfg.overheat.enabled and ka <= 0:
            self._sleep(seconds)
            return
        now = self._clock()
        end = now + seconds
        next_ka = now + ka
        next_temp = now
        while now < end:
            if ka > 0 and now >= next_ka:
                for link, prof in running:
                    link.set_speed(prof.speed, prof.accel, prof.current, retry=False)
                next_ka = now + ka
            if cfg.overheat.enabled and now >= next_temp:
                self._check_overheat()
                next_temp = now + cfg.overheat.check_interval_s
            self._sleep(min(cfg.hold_slice_s, end - now))
            now = self._clock()

    def _read_temps(self) -> dict[str, tuple[int, int] | None]:
        temps: dict[str, tuple[int, int] | None] = {}
        for key, link in (("tested", self.tested), ("load", self.load)):
            value = link.read_temperatures()
            temps[key] = value
            if value is not None:
                self.last_temps[key] = value
        return temps

    def _hot_reason(self, temps: Mapping[str, tuple[int, int] | None]) -> str | None:
        oh = self.cfg.overheat
        for key, value in temps.items():
            if value is None:
                continue
            mos, motor = value
            if mos > oh.mos_limit or motor > oh.motor_limit:
                label = self.tested.label if key == "tested" else self.load.label
                return f"{label} MOSFET:{mos}℃ 电机:{motor}℃"
        return None

    def _check_overheat(self) -> None:
        reason = self._hot_reason(self._read_temps())
        if reason:
            raise _OverheatInterrupt(reason)

    def _precheck_temperature(self) -> None:
        reason = self._hot_reason(self._read_temps())
        if reason or self._overheat_pending:
            if reason:
                log.warning("轮次开始前温度超限: %s", reason)
            self._cooldown()

    def _cooldown(self) -> None:
        oh = self.cfg.overheat
        self.cooldowns += 1
        log.warning(">>> 超温暂停：电机已停止，等待双方温度恢复 (MOSFET<%g℃ 且 电机<%g℃)...",
                    oh.mos_recover, oh.motor_recover)
        self._stop_all(lock=False)
        start = self._clock()
        failures = 0
        while True:
            temps = self._read_temps()
            if any(v is None for v in temps.values()):
                failures += 1
                log.warning("冷却期间温度读取失败（连续 %d 次）", failures)
                if failures >= oh.max_read_failures:
                    raise TestAbort(f"温度连续读取失败 {failures} 次，无法监控温度")
            else:
                failures = 0
                if all(v[0] < oh.mos_recover and v[1] < oh.motor_recover for v in temps.values() if v):
                    log.info("温度已恢复，继续测试: %s", temps)
                    self._overheat_pending = False
                    return
                log.info("冷却中: %s", temps)
            if oh.cooldown_timeout_s > 0 and self._clock() - start >= oh.cooldown_timeout_s:
                raise TestAbort(f"超温冷却超过 {oh.cooldown_timeout_s:g}s 仍未恢复")
            self._sleep(oh.cooldown_poll_s)

    def _stop_all(self, *, lock: bool) -> None:
        """尽力把双机恢复到安全状态，任何异常只记录。"""
        for link in (self.tested, self.load):
            for _ in range(3):
                try:
                    if link.set_speed(0, self.cfg.stop_accel, 0, retry=False):
                        break
                except ToolError as exc:
                    log.error("%s停机失败: %s", link.label, exc)
                    break
                self._sleep(0.1)
        if lock:
            try:
                self._lock_command()
            except ToolError as exc:
                log.error("%s上锁失败（请人工确认刹车状态）: %s", self.tested.label, exc)


# ============================================================ 应用入口
CodecFactory = Callable[[Path], Any]

REPORT_COLUMNS = ["index", "verdict", "detail", "load_started", "unlock_ok", "unlock_ms", "unlock_err",
                  "unlock_brake", "run_time_s", "lock_ok", "lock_brake", "overheat", "tested_temp", "load_temp"]


def _default_codec(dll_path: Path) -> RegionCodecV2:
    return RegionCodecV2(PpxDll(dll_path))


def write_reports(ctx: RunContext, cycle: BrakeLockUnlockCycle, summary: RunSummary) -> None:
    if ctx.run_dir is None or not cycle.rows:
        return
    write_csv(ctx.run_dir / "brake_lock_unlock.csv", cycle.rows, REPORT_COLUMNS)
    head = {
        "目标轮数": summary.target_cycles, "已执行": summary.executed,
        "通过": summary.passed, "失败": summary.failed, **cycle.stats(),
    }
    if summary.abort_reason:
        head["熔断原因"] = summary.abort_reason
    if summary.error:
        head["程序异常"] = summary.error
    write_html(ctx.run_dir / "brake_lock_unlock.html", title="电磁刹车解锁/上锁成功率测试", summary=head,
               rows=cycle.rows, columns=REPORT_COLUMNS)


def run(
    settings: AppSettings,
    ctx: RunContext,
    *,
    codec_factory: CodecFactory | None = None,
    serial_factory: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: random.Random | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    proto = settings.section("protocol", RegionProtocolConfig)
    cfg = settings.section("brake_test", BrakeTestConfig)
    make_codec = codec_factory or _default_codec
    tested_codec = make_codec(proto.dll_path)
    load_codec = make_codec(proto.dll_path)

    with contextlib.ExitStack() as stack:
        tested_tr = open_serial(settings, "tested", factory=serial_factory)
        stack.callback(tested_tr.close)
        load_tr = open_serial(settings, "load", exclude=[tested_tr.port], factory=serial_factory)
        stack.callback(load_tr.close)

        def _link(codec: Any, transport: Any, label: str, name: str) -> MotorLink:
            client = RegionClient(codec, transport, dev_id=proto.dev_id, rx_timeout_s=proto.rx_timeout_s,
                                  retries=proto.retries, retry_delay_s=proto.retry_delay_s, name=name)
            return MotorLink(client, label, retries=cfg.command_retries, retry_delay_s=cfg.command_retry_delay_s,
                             reconnect_delay_s=cfg.reconnect_delay_s, sleep=sleep)

        cycle = BrakeLockUnlockCycle(
            _link(tested_codec, tested_tr, "被测电机", "tested"),
            _link(load_codec, load_tr, "负载电机", "load"),
            cfg, sleep=sleep, clock=clock, rng=rng,
        )
        log.info("电磁刹车解锁/上锁成功率测试：被测 %s | 负载 %s | 目标 %d 轮 | 超温保护 %s",
                 tested_tr.port, load_tr.port, cfg.cycles, "开启" if cfg.overheat.enabled else "关闭")
        runner = StressRunner(
            cycle, cfg.cycles, station=settings.station, stop_on_fail=cfg.stop_on_fail,
            max_consecutive_failures=cfg.max_consecutive_failures or None,
            interval_s=cfg.round_interval_s, stop_event=stop_event,
        )
        summary = runner.run()

    write_reports(ctx, cycle, summary)
    return 0 if summary.ok else 1
