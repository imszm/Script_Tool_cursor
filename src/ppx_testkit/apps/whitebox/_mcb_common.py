"""L5 MCB 白盒类应用的公共部分：链路配置、连接建立、影子寄存器、报告输出。"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ppx_testkit.core.factory import open_profile_region
from ppx_testkit.core.protocol.heartbeat import ShadowReg
from ppx_testkit.core.protocol.ppx_types import Msg
from ppx_testkit.core.protocol.region_client import RegionClient
from ppx_testkit.core.report.writers import write_detail_reports
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.logger import RunContext
from ppx_testkit.profiles.l5 import L5
from ppx_testkit.profiles.l5 import mcb_shadow_regs as _l5_shadow_regs
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def _default_region_dll() -> Path:
    """L5 region DLL。路径和协议版本定义在 profiles.l5。"""
    return L5.dll_file("region")


@dataclass(frozen=True)
class McbLinkConfig:
    """MCB 通信链路（``link`` 段）。节点和超时默认取 L5 profile。"""

    dll: Path = field(default_factory=_default_region_dll)
    serial: str = "mcb"  # 对应 serial.<name> 端点
    dev_id: int = L5.dev_id
    rx_timeout_s: float = L5.rx_timeout_s
    retries: int = L5.retries
    retry_delay_s: float = L5.retry_delay_s

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
    """按 L5 profile 加载 region DLL 并打开 MCB 串口；退出时保证关闭串口。

    先加载 DLL 再打开串口：DLL 缺失时不会占用端口。协议版本取自 L5，不在这里写 v1。
    """
    with open_profile_region(
        settings,
        L5,
        serial_name=link.serial,
        dll=link.dll,
        dev_id=link.dev_id,
        rx_timeout_s=link.rx_timeout_s,
        retries=link.retries,
        retry_delay_s=link.retry_delay_s,
    ) as client:
        yield client


def mcb_shadow_regs() -> list[ShadowReg]:
    """返回 L5 心跳寄存器表。顺序定义在 profiles.l5。

    :return: 影子寄存器列表。
    """
    return _l5_shadow_regs()


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
    client.write_raw(reg, fields, nums=nums, cmd=cmd, label=label)


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
    write_detail_reports(
        ctx, stem, title=title, rows=rows, columns=columns, summary=summary, summary_name="summary.json",
    )


def count_verdicts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {PASS: 0, FAIL: 0, SKIP: 0}
    for row in rows:
        verdict = str(row.get("verdict", ""))
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts
