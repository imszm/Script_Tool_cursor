"""L5 MCB 霍尔传感器体检（迁移自 ``mcb_V1.4.7.py``，原“硬件诊断工具 V2.9”）。

用于排查 0x040000 / 0x240000 故障的物理根源：不给电机通电，只轮询 HALL_STATE。

操作：运行后看到“开始监听”提示，**用手用力转动电机轮子**。

判定：
* 正常：霍尔值在 1~6 之间快速跳变，跳变次数 >= ``required_transitions`` 即 PASS；
* 故障：一直为 0 / 7（插头松脱或断线）或不变化（传感器损坏）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ppx_testkit.apps.whitebox._mcb_common import (
    FAIL,
    PASS,
    Connector,
    McbLinkConfig,
    RegionLike,
    open_region_client,
    write_reports,
)
from ppx_testkit.core.protocol.ppx_types import RegV1
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.logger import RunContext, get_logger
from ppx_testkit.settings import AppSettings
from ppx_testkit.utils.timing import Deadline

log = get_logger(__name__)

REPORT_STEM = "mcb_hall_diag"
REPORT_COLUMNS = ["index", "t_s", "hall_state", "status", "verdict"]
VALID_HALL_STATES = range(1, 7)


@dataclass(frozen=True)
class HallDiagConfig:
    duration_s: float = 20.0
    poll_interval_s: float = 0.1
    # 旧脚本判据为“跳变次数 > 5”
    required_transitions: int = 6
    voltage_scale: float = 0.1
    max_consecutive_read_failures: int = 50

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ConfigError("hall_diag.duration_s 必须 > 0")
        if self.poll_interval_s < 0:
            raise ConfigError("hall_diag.poll_interval_s 不能为负数")
        if self.required_transitions < 1:
            raise ConfigError("hall_diag.required_transitions 至少为 1")
        if self.voltage_scale <= 0:
            raise ConfigError("hall_diag.voltage_scale 必须 > 0")
        if self.max_consecutive_read_failures < 1:
            raise ConfigError("hall_diag.max_consecutive_read_failures 至少为 1")


@dataclass
class HallDiagResult:
    verdict: str
    detail: str
    bus_voltage_v: float | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)
    valid_transitions: int = 0
    samples: int = 0
    read_failures: int = 0
    elapsed_s: float = 0.0


def hall_status(state: int) -> str:
    return "正常" if state in VALID_HALL_STATES else "异常:断线/非法"


def diagnose_hall(
    client: RegionLike,
    cfg: HallDiagConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> HallDiagResult:
    raw_volt = client.try_read_field(RegV1.BUS_VOLTAGE, "bus_voltage", label="读母线电压")
    # 与旧脚本一致：读数为 0 同样视为供电/通信异常
    if not raw_volt:
        return HallDiagResult(FAIL, f"无法读取电压，通信中断 (读数 {raw_volt!r})")
    volt = raw_volt * cfg.voltage_scale
    log.info("当前电压: %.1fV (硬件供电正常)", volt)

    result = HallDiagResult(FAIL, "", bus_voltage_v=round(volt, 2))
    log.info("开始监听霍尔信号 (持续 %.0f 秒)...", cfg.duration_s)
    log.info("请现在【用手用力转动】电机轮子！")

    deadline = Deadline(cfg.duration_s, clock=clock)
    last_hall: int | None = None
    consecutive_failures = 0
    aborted = False
    while not deadline.expired():
        hall = client.try_read_field(RegV1.HALL_STATE, "hall_state", label="读霍尔状态")
        if hall is None:
            result.read_failures += 1
            consecutive_failures += 1
            if consecutive_failures >= cfg.max_consecutive_read_failures:
                log.error("连续 %d 次读取霍尔状态失败，终止诊断", consecutive_failures)
                aborted = True
                break
        else:
            consecutive_failures = 0
            result.samples += 1
            if hall != last_hall:
                status = hall_status(hall)
                t_s = round(deadline.elapsed, 2)
                log.info("%6.1fs | 霍尔状态 %d (%s)", t_s, hall, status)
                result.transitions.append({
                    "index": len(result.transitions) + 1,
                    "t_s": t_s,
                    "hall_state": hall,
                    "status": status,
                    "verdict": PASS if hall in VALID_HALL_STATES else FAIL,
                })
                last_hall = hall
                if hall in VALID_HALL_STATES:
                    result.valid_transitions += 1
        sleep(cfg.poll_interval_s)
    result.elapsed_s = round(deadline.elapsed, 2)

    if aborted:
        result.verdict = FAIL
        result.detail = f"通信中断：连续 {consecutive_failures} 次读取霍尔状态失败"
    elif result.valid_transitions >= cfg.required_transitions:
        result.verdict = PASS
        result.detail = (
            f"霍尔传感器工作正常（检测到 {result.valid_transitions} 次有效跳变）。"
            "硬件连接无问题，若仍报故障请检查相线（黄/绿/蓝粗线）线序"
        )
    else:
        result.verdict = FAIL
        result.detail = (
            f"霍尔传感器无反应（有效跳变 {result.valid_transitions} 次，需 >= {cfg.required_transitions}）："
            "霍尔线(细线)未插好或传感器已损坏"
        )
    return result


def run(settings: AppSettings, ctx: RunContext, *, connect: Connector = open_region_client) -> int:
    link = settings.section("link", McbLinkConfig, required=False)
    cfg = settings.section("hall_diag", HallDiagConfig, required=False)

    log.info("=" * 50)
    log.info("MCB 霍尔传感器体检 | DLL: %s | 串口端点: serial.%s", link.dll, link.serial)
    log.info("=" * 50)

    result: HallDiagResult | None = None
    try:
        with connect(settings, link) as client:
            result = diagnose_hall(client, cfg)
    finally:
        summary = {
            "工位": settings.station,
            "结论": result.verdict if result else FAIL,
            "说明": result.detail if result else "未完成诊断（连接失败或被中断）",
            "母线电压(V)": result.bus_voltage_v if result else None,
            "有效跳变次数": result.valid_transitions if result else 0,
            "采样次数": result.samples if result else 0,
            "读取失败次数": result.read_failures if result else 0,
            "监听时长(s)": result.elapsed_s if result else 0,
        }
        write_reports(ctx, REPORT_STEM, title="MCB 霍尔传感器体检报告",
                      rows=result.transitions if result else [], columns=REPORT_COLUMNS, summary=summary)

    if result is None:
        log.error("诊断未完成：未能建立连接或诊断过程被中断")
        return 1
    log.log(logging.INFO if result.verdict == PASS else logging.ERROR, "诊断结果 [%s]: %s", result.verdict,
            result.detail)
    return 0 if result.verdict == PASS else 1
