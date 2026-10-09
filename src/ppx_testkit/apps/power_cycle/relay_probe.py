"""继电器指令/极性人工探测小工具。

对应旧脚本 ``Tool/查看继电器状态.py``，工位配置 ``config/stations/relay_probe.yaml``。

依次发送 ``test.steps`` 中的继电器命名指令（``relay.commands``），每步提示操作员观察
继电器是否吸合 / 指示灯状态，按回车继续。用于更换继电器模块后确认 on/off 字节极性，
再据此填写各压测工位的 ``relay.channels``。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ppx_testkit.apps.power_cycle._common import close_ports, open_ports, require_non_negative
from ppx_testkit.core.relay.base import RelayConfig, RelayDriver, build_relay
from ppx_testkit.core.serial.port_finder import PortFinder
from ppx_testkit.core.serial.transport import SerialFactory
from ppx_testkit.exceptions import ConfigError, HardwareError
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)

SECTION = "test"


@dataclass(frozen=True)
class ProbeStep:
    command: str                 # relay.commands 中的指令名
    expect: str = ""             # 期望观察到的现象（提示操作员）


@dataclass(frozen=True)
class RelayProbeConfig:
    steps: list[ProbeStep] = field(default_factory=list)
    interactive: bool = True     # true：每步之后等待回车；false：等待 step_wait_s 后自动继续
    step_wait_s: float = 2.0

    def __post_init__(self) -> None:
        if not self.steps:
            raise ConfigError(f"{SECTION}.steps 不能为空")
        require_non_negative(SECTION, step_wait_s=self.step_wait_s)


def probe(
    cfg: RelayProbeConfig,
    relay: RelayDriver,
    *,
    prompt: Callable[[str], str] = input,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """执行探测步骤；返回 0 表示全部指令发送成功，1 表示中途失败或被操作员中止。"""
    missing = [s.command for s in cfg.steps if s.command not in relay.cfg.commands]
    if missing:
        raise ConfigError(f"test.steps 引用了未在 relay.commands 中定义的指令: {missing}")
    total = len(cfg.steps)
    for i, step in enumerate(cfg.steps, 1):
        log.info("[%d/%d] 发送 '%s'%s", i, total, step.command, f"（期望: {step.expect}）" if step.expect else "")
        try:
            relay.send(step.command)
        except HardwareError as exc:
            log.error("继电器指令 '%s' 发送失败: %s", step.command, exc)
            return 1
        if i == total:
            break
        if cfg.interactive:
            try:
                prompt(f"请观察继电器状态，按回车发送下一条指令 '{cfg.steps[i].command}'...")
            except (EOFError, KeyboardInterrupt):
                log.warning("操作员中止探测")
                return 1
        else:
            sleep(cfg.step_wait_s)
    log.info("全部 %d 条指令发送完成，请根据观察结果确认各工位 relay.channels 的 on/off 字节", total)
    return 0


def run(
    settings: AppSettings,
    ctx: RunContext,
    *,
    factory: SerialFactory | None = None,
    finder: PortFinder | None = None,
    prompt: Callable[[str], str] = input,
) -> int:
    cfg = settings.section(SECTION, RelayProbeConfig)
    relay_cfg = settings.section("relay", RelayConfig)
    log.info("运行目录: %s", ctx.run_dir or "<仅控制台>")
    ports = open_ports(settings, [("relay", not relay_cfg.open_per_command)], factory=factory, finder=finder)
    try:
        return probe(cfg, build_relay(relay_cfg, ports["relay"]), prompt=prompt)
    finally:
        close_ports(*ports.values())
