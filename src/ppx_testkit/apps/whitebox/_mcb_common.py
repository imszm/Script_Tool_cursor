"""L5 MCB 白盒类应用的公共部分：链路配置、连接建立、影子寄存器、报告输出。"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ppx_testkit.core.factory import open_serial
from ppx_testkit.core.protocol.dll_loader import PpxDll
from ppx_testkit.core.protocol.heartbeat import ShadowReg
from ppx_testkit.core.protocol.ppx_types import DevId, Msg, RegV1
from ppx_testkit.core.protocol.region_client import RegionClient, RegionCodecV1
from ppx_testkit.core.report.writers import write_csv, write_html
from ppx_testkit.exceptions import ConfigError, ProtocolError
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings
from ppx_testkit.utils.paths import resources_dir

log = logging.getLogger(__name__)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def _default_region_dll() -> Path:
    return resources_dir() / "dll" / "l5" / "ppx_region.dll"


@dataclass(frozen=True)
class McbLinkConfig:
    """MCB 通信链路（``link`` 段）。"""

    dll: Path = field(default_factory=_default_region_dll)
    serial: str = "mcb"  # 对应 serial.<name> 端点
    dev_id: int = DevId.MCB
    rx_timeout_s: float = 0.3
    retries: int = 3
    retry_delay_s: float = 0.1

    def __post_init__(self) -> None:
        if not self.serial:
            raise ConfigError("link.serial 不能为空")
        if not 0 <= self.dev_id <= 0xFF:
            raise ConfigError(f"link.dev_id 超出范围: {self.dev_id}")
        if self.rx_timeout_s <= 0:
            raise ConfigError("link.rx_timeout_s 必须 > 0")
        if self.retries < 1:
            raise ConfigError("link.retries 至少为 1")
        if self.retry_delay_s < 0:
            raise ConfigError("link.retry_delay_s 不能为负数")


class RegionLike(Protocol):
    """测试用例只依赖的 RegionClient 子集，便于用假对象做单元测试。"""

    def try_read_field(self, reg: int, field: str, *, label: str = "") -> Any | None: ...

    def write(
        self,
        reg: int,
        fields: Mapping[str, Any],
        *,
        nums: int = 1,
        expect_response: bool = True,
        label: str = "",
    ) -> Any: ...


Connector = Callable[[AppSettings, McbLinkConfig], contextlib.AbstractContextManager[RegionClient]]


@contextlib.contextmanager
def open_region_client(settings: AppSettings, link: McbLinkConfig) -> Iterator[RegionClient]:
    """加载 L5 region DLL 并打开 MCB 串口；退出时保证关闭串口。

    先加载 DLL 再打开串口：DLL 缺失时不会占用端口。
    """
    codec = RegionCodecV1(PpxDll(link.dll))
    transport = open_serial(settings, link.serial)
    try:
        client = RegionClient(
            codec,
            transport,
            dev_id=link.dev_id,
            rx_timeout_s=link.rx_timeout_s,
            retries=link.retries,
            retry_delay_s=link.retry_delay_s,
            name=link.serial,
        )
        yield client
    finally:
        transport.close()


def mcb_shadow_regs() -> list[ShadowReg]:
    """L5 MCB 测试模式下需周期刷新的控制寄存器（顺序与旧脚本心跳一致）。"""
    return [
        ShadowReg(RegV1.RUN_MODE, "run_mode", skip_when_zero=True),
        ShadowReg(RegV1.RT_SETTING, "rt_setting"),
        ShadowReg(RegV1.TARGET_SPEED, "target_speed"),
        ShadowReg(RegV1.DAT_SETTING, "dat_setting", skip_when_zero=True),
    ]


def write_raw(
    client: RegionClient,
    reg: int,
    fields: Mapping[str, Any],
    *,
    nums: int = 1,
    cmd: int = Msg.WRITE,
    label: str = "",
) -> None:
    """按指定命令字写寄存器且不等待应答。

    旧脚本对 32 位寄存器（加速度）使用 ``cmd=WRITE, reg_nums=2`` 下发，而
    :meth:`RegionClient.write` 在 ``nums > 1`` 时会改用 MULTWRITE，故在此保留旧报文格式。
    串口/组包异常（HardwareError / ProtocolError）原样抛出，由用例层判定失败。
    """
    with client.lock:
        data = client.codec.tx_data()
        for name, value in fields.items():
            if not hasattr(data, name):
                raise ProtocolError(f"寄存器数据结构没有字段 '{name}'")
            setattr(data, name, value)
        request = client.codec.format(client.dev_id, cmd, reg, nums)
        client.log.debug("[%s] TX cmd=0x%02X reg=%d nums=%d %s", label or f"写寄存器{reg}", cmd, reg, nums,
                         request.hex(" ").upper())
        client.transport.write(request)


def write_reports(
    ctx: RunContext,
    stem: str,
    *,
    title: str,
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    summary: Mapping[str, Any],
) -> None:
    """在 ctx.run_dir 下输出 <stem>.csv / <stem>.html 与 summary.json。"""
    if ctx.run_dir is None:
        log.warning("运行目录不可用，跳过报告输出（%s）", ctx.fallback_reason or "未知原因")
        return
    write_csv(ctx.run_dir / f"{stem}.csv", rows, columns)
    write_html(ctx.run_dir / f"{stem}.html", title=title, summary=summary, rows=rows, columns=columns,
               verdict_key="verdict")
    ctx.write_json("summary.json", dict(summary))


def count_verdicts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {PASS: 0, FAIL: 0, SKIP: 0}
    for row in rows:
        verdict = str(row.get("verdict", ""))
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts
