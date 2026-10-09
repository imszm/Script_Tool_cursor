"""L5 灯板/CCB BLE 协议 LED 自动化测试（迁移自 ``ble_自动化测试工具（测试版本）-V1.6.py``）。

流程：
1. 从 Excel/CSV 加载用例（或 ``combo: true`` 自动生成排列组合用例）；
2. 每条用例：写 LED 寄存器 -> 读 LED 寄存器 -> 按 ``expect_*`` 列断言；
3. 可选压力循环：对第 1 条用例重复下发 ``loop_count`` 次；
4. 输出 ``lcb_ble.csv`` / ``lcb_ble.html`` / ``summary.json``。

用例列：
* 设置：screen_on, brightness, digital, logo, rim_state, rdygo, turn_left, turn_right, ring
  （screen_on 留空默认 1，其余留空默认 0）；
* 断言：expect_<字段>，留空表示不校验该字段；
* 控制：id, comment, recv_timeout(秒，留空/0 用默认), delay_after(秒)。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import itertools
import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ppx_testkit.apps.whitebox._mcb_common import FAIL, PASS, SKIP, count_verdicts, write_reports
from ppx_testkit.core.factory import open_serial
from ppx_testkit.core.protocol.ble_client import BleClient, BleCodecV1, BleResult
from ppx_testkit.core.protocol.dll_loader import PpxDll
from ppx_testkit.core.protocol.ppx_types import LED_FIELDS_V1, DevId
from ppx_testkit.core.report.case_loader import load_cases, to_bool_int, to_float, to_int
from ppx_testkit.exceptions import ConfigError, HardwareError, ProtocolError, SerialDisconnectedError
from ppx_testkit.logger import RunContext, get_logger
from ppx_testkit.settings import AppSettings
from ppx_testkit.utils.paths import resources_dir

log = get_logger(__name__)

REPORT_STEM = "lcb_ble"
REPORT_COLUMNS = [
    "case_id", "comment", *LED_FIELDS_V1, "recv_timeout", "delay_after",
    "send_ok", "read_ok", "recv_hex", "read_hex", "parse_send", "parse_read",
    "checks", "expect_detail", "verdict", "error", "elapsed_s", "timestamp",
]

#: 旧脚本 make_combo_cases 的取值范围（仅保留 screen_on=1 且 brightness>=7 的组合）
COMBO_FIELDS: dict[str, tuple[int, ...]] = {
    "screen_on": (0, 1),
    "brightness": (0, 1, 2, 3, 4, 5, 6, 7),
    "digital": (0, 50, 100),
    "logo": (0, 1, 2),
    "rim_state": (0, 1, 2),
    "rdygo": (0, 1, 2),
    "turn_left": (0, 1, 2),
    "turn_right": (0, 1, 2),
    "ring": (0, 1, 2),
}


# ============================================================ 配置
def _default_ble_dll() -> Path:
    return resources_dir() / "dll" / "l5" / "ppx_ble.dll"


def _default_cases() -> Path:
    return resources_dir() / "testcases" / "testcases.xlsx"


@dataclass(frozen=True)
class BleLinkConfig:
    dll: Path = field(default_factory=_default_ble_dll)
    serial: str = "lcb"
    dev_id: int = DevId.BLE
    rx_timeout_s: float = 1.0

    def __post_init__(self) -> None:
        if not self.serial:
            raise ConfigError("link.serial 不能为空")
        if not 0 <= self.dev_id <= 0xFF:
            raise ConfigError(f"link.dev_id 超出范围: {self.dev_id}")
        if self.rx_timeout_s <= 0:
            raise ConfigError("link.rx_timeout_s 必须 > 0")


@dataclass(frozen=True)
class LcbBleConfig:
    cases: Path = field(default_factory=_default_cases)
    combo: bool = False
    loop_count: int = 1
    loop_delay_s: float = 0.5
    title: str = "BLE 自动化测试报告"

    def __post_init__(self) -> None:
        if self.loop_count < 0:
            raise ConfigError("lcb_ble.loop_count 不能为负数")
        if self.loop_delay_s < 0:
            raise ConfigError("lcb_ble.loop_delay_s 不能为负数")


# ============================================================ 用例逻辑
class LedClientLike(Protocol):
    def set_led(self, values: Mapping[str, int], *, timeout_s: float | None = None) -> BleResult: ...

    def read_led(self, *, timeout_s: float | None = None) -> BleResult: ...


def make_combo_cases() -> list[dict[str, Any]]:
    keys = list(COMBO_FIELDS)
    cases: list[dict[str, Any]] = []
    for values in itertools.product(*(COMBO_FIELDS[k] for k in keys)):
        row = dict(zip(keys, values, strict=True))
        if not (row["screen_on"] == 1 and row["brightness"] >= 7):
            continue
        row["id"] = len(cases) + 1
        cases.append(row)
    return cases


def case_params(row: Mapping[str, Any]) -> dict[str, int]:
    """用例行 -> LED 设置值（screen_on 留空默认 1，其余留空默认 0）。"""
    screen_on = to_bool_int(row.get("screen_on"))
    params = {"screen_on": 1 if screen_on is None else screen_on}
    for name in LED_FIELDS_V1:
        if name != "screen_on":
            params[name] = to_int(row.get(name)) or 0
    return params


def check_led_expectations(row: Mapping[str, Any], led: Mapping[str, int] | None) -> tuple[str, str]:
    """按 expect_* 列校验回读的 LED 状态；留空的列不校验。"""
    if led is None:
        return FAIL, "未能读取到 LED 状态"
    fail_msgs = []
    checked = 0
    for name in LED_FIELDS_V1:
        raw = row.get(f"expect_{name}")
        expected = to_bool_int(raw) if name == "screen_on" else to_int(raw)
        if expected is None:
            continue
        checked += 1
        actual = led.get(name)
        if actual != expected:
            fail_msgs.append(f"{name}: 实际={actual}, 期望={expected}")
    if fail_msgs:
        return FAIL, "; ".join(fail_msgs)
    return PASS, "全部匹配" if checked else "未配置期望值（不校验）"


def _hex(data: bytes | None) -> str:
    return data.hex(" ").upper() if data else ""


@dataclass
class CaseRun:
    row: dict[str, Any]
    fatal: bool = False


def run_case(client: LedClientLike, row: Mapping[str, Any], index: int) -> CaseRun:
    case_id = row.get("id")
    case_id = index if case_id in (None, "") else case_id
    comment = str(row.get("comment") or "")
    params = case_params(row)
    recv_timeout = to_float(row.get("recv_timeout"))
    delay_after = to_float(row.get("delay_after")) or 0.0
    # 与旧脚本一致：recv_timeout 为空或 0 时使用链路默认超时
    timeout_s = recv_timeout or None

    log.info("==== 执行用例 #%s ==== 参数: %s | 备注: %s", case_id, params, comment)
    start = time.monotonic()
    write_res: BleResult | None = None
    read_res: BleResult | None = None
    error: str | None = None
    fatal = False
    try:
        write_res = client.set_led(params, timeout_s=timeout_s)
        read_res = client.read_led(timeout_s=timeout_s)
    except SerialDisconnectedError as exc:
        error, fatal = f"串口断开: {exc}", True
    except (HardwareError, ProtocolError) as exc:
        error = f"通信/协议异常: {exc}"

    send_ok = bool(write_res and write_res.ok)
    read_ok = bool(read_res and read_res.ok)
    led = read_res.led if read_ok and read_res else None
    expect_verdict, expect_detail = check_led_expectations(row, led)
    if error is None and not send_ok:
        error = (write_res.error if write_res else None) or "写 LED 失败"
    elif error is None and not read_ok:
        error = (read_res.error if read_res else None) or "读 LED 失败"
    # BleClient 把串口断开转换成失败结果而不是抛出，这里仍要中止后续用例
    if not fatal and error and "SerialDisconnectedError" in error:
        error, fatal = f"串口断开: {error}", True
    verdict = PASS if (error is None and send_ok and read_ok and expect_verdict == PASS) else FAIL
    elapsed = time.monotonic() - start
    log.log(logging.INFO if verdict == PASS else logging.ERROR, "结果: %s | %s | 用时 %.3fs",
            verdict, error or expect_detail, elapsed)

    result_row: dict[str, Any] = {
        "case_id": case_id,
        "comment": comment,
        **params,
        "recv_timeout": recv_timeout,
        "delay_after": delay_after,
        "send_ok": PASS if send_ok else FAIL,
        "read_ok": PASS if read_ok else FAIL,
        "recv_hex": _hex(write_res.response if write_res else None),
        "read_hex": _hex(read_res.response if read_res else None),
        "parse_send": write_res.parse_status if write_res else None,
        "parse_read": read_res.parse_status if read_res else None,
        "checks": led or {},
        "expect_detail": expect_detail,
        "verdict": verdict,
        "error": error,
        "elapsed_s": round(elapsed, 3),
        "timestamp": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return CaseRun(result_row, fatal)


def run_cases(
    client: LedClientLike,
    cases: Sequence[Mapping[str, Any]],
    out: list[dict[str, Any]],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """执行全部用例，结果逐条追加到 ``out``（中断时可保留已完成部分）。

    串口断开后其余用例记为 SKIP，返回中止原因；正常执行完返回 None。
    """
    aborted: str | None = None
    for idx, row in enumerate(cases, start=1):
        if aborted:
            out.append({
                "case_id": row.get("id") or idx, "comment": str(row.get("comment") or ""),
                "verdict": SKIP, "error": f"已跳过：{aborted}",
            })
            continue
        case = run_case(client, row, idx)
        out.append(case.row)
        if case.fatal:
            aborted = "串口断开"
            continue
        delay = case.row["delay_after"]
        if delay > 0:
            sleep(delay)
    return aborted


@dataclass
class LoopStats:
    total: int = 0
    ok: int = 0
    aborted: str | None = None

    @property
    def failed(self) -> int:
        return self.total - self.ok


def loop_case(
    client: LedClientLike,
    row: Mapping[str, Any],
    count: int,
    delay_s: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> LoopStats:
    """压力循环：重复下发同一组 LED 设置，以应答是否成功计数。"""
    stats = LoopStats()
    params = case_params(row)
    log.info("开始压力循环: 次数=%d, 间隔=%.2fs, 参数=%s", count, delay_s, params)
    for i in range(1, count + 1):
        stats.total += 1
        try:
            res = client.set_led(params)
            ok = res.ok
            detail = res.error or ""
            if not ok and "SerialDisconnectedError" in detail:
                log.error("[循环 %d/%d] 串口断开，终止压力循环: %s", i, count, detail)
                stats.aborted = "串口断开"
                break
        except SerialDisconnectedError as exc:
            log.error("[循环 %d/%d] 串口断开，终止压力循环: %s", i, count, exc)
            stats.aborted = "串口断开"
            break
        except (HardwareError, ProtocolError) as exc:
            ok, detail = False, str(exc)
        if ok:
            stats.ok += 1
        log.log(logging.INFO if ok else logging.ERROR, "[循环 %d/%d] 发送 %s %s", i, count,
                "OK" if ok else "FAIL", detail)
        if i < count and delay_s > 0:
            sleep(delay_s)
    return stats


# ============================================================ 入口
BleConnector = Callable[[AppSettings, BleLinkConfig], contextlib.AbstractContextManager[LedClientLike]]


@contextlib.contextmanager
def open_ble_client(settings: AppSettings, link: BleLinkConfig) -> Iterator[BleClient]:
    """加载 L5 BLE DLL 并打开串口；退出时保证关闭串口。"""
    codec = BleCodecV1(PpxDll(link.dll))
    transport = open_serial(settings, link.serial)
    try:
        yield BleClient(codec, transport, dev_id=link.dev_id, rx_timeout_s=link.rx_timeout_s)
    finally:
        transport.close()


def run(settings: AppSettings, ctx: RunContext, *, connect: BleConnector = open_ble_client) -> int:
    link = settings.section("link", BleLinkConfig, required=False)
    cfg = settings.section("lcb_ble", LcbBleConfig, required=False)

    # 先加载用例：用例文件有问题时不占用串口
    if cfg.combo:
        cases: list[dict[str, Any]] = make_combo_cases()
        source = "排列组合生成"
    else:
        cases = load_cases(cfg.cases)
        source = str(cfg.cases)
    log.info("加载用例 %d 条，来源: %s", len(cases), source)
    log.info("BLE DLL: %s | 串口端点: serial.%s", link.dll, link.serial)

    results: list[dict[str, Any]] = []
    loop = LoopStats()
    aborted: str | None = None
    started = time.monotonic()
    try:
        with connect(settings, link) as client:
            aborted = run_cases(client, cases, results)
            if cfg.loop_count > 0 and cases and aborted is None:
                loop = loop_case(client, cases[0], cfg.loop_count, cfg.loop_delay_s)
    finally:
        counts = count_verdicts(results)
        executed = counts[PASS] + counts[FAIL]
        summary = {
            "工位": settings.station,
            "用例来源": source,
            "总用例": len(cases),
            "通过": counts[PASS],
            "失败": counts[FAIL],
            "跳过": counts[SKIP] + (len(cases) - len(results)),
            "通过率": f"{(counts[PASS] / executed * 100) if executed else 0.0:.2f}%",
            "中止原因": aborted,
            "压力循环(成功/总数)": f"{loop.ok}/{loop.total}",
            "压力循环中止原因": loop.aborted,
            "耗时(s)": round(time.monotonic() - started, 1),
        }
        write_reports(ctx, REPORT_STEM, title=cfg.title, rows=results, columns=REPORT_COLUMNS, summary=summary)
        log.info("测试结束：通过 %d / 失败 %d / 跳过 %d；压力循环 %d/%d",
                 counts[PASS], counts[FAIL], summary["跳过"], loop.ok, loop.total)

    all_pass = len(results) == len(cases) and counts[PASS] == len(cases) and loop.failed == 0 and not loop.aborted
    return 0 if all_pass else 1
