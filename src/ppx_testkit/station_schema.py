"""按应用注册工位配置段，在 load_settings 返回前完成类型校验。

未知应用不校验业务段（测试夹具和尚未注册的模块仍可加载）。
已出现的 serial / relay / relays / keywords 无论应用是否注册都会校验。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from ppx_testkit.core.monitor.keyword_rules import KeywordConfig
from ppx_testkit.core.relay.base import RelayConfig
from ppx_testkit.core.serial.transport import SerialEndpoint
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.settings import from_dict


@dataclass(frozen=True)
class SectionRule:
    """一个配置段：key 为点号路径的第一段，cls 为对应 dataclass。"""

    key: str
    cls: type
    required: bool = True


@dataclass(frozen=True)
class AppSchema:
    """某个 app 在启动时必须满足的配置契约。"""

    sections: tuple[SectionRule, ...] = ()
    serial_names: tuple[str, ...] = ()
    relay: bool = False
    relays: bool = False


def validate_station(app: str, data: Mapping[str, Any]) -> None:
    """校验合并后的工位配置。类型不符、未知字段或缺少必填段时抛出 ConfigError。"""
    _validate_serial(data, _registry().get(app))
    _validate_relay(data, _registry().get(app))
    _validate_keywords(data)
    schema = _registry().get(app)
    if schema is None:
        return
    for rule in schema.sections:
        present = isinstance(data, Mapping) and rule.key in data and data[rule.key] is not None
        if rule.required and not present:
            raise ConfigError(f"缺少配置段 '{rule.key}'（应用 {app}）")
        if present:
            from_dict(rule.cls, data[rule.key], rule.key)


def _validate_serial(data: Mapping[str, Any], schema: AppSchema | None) -> None:
    serial = data.get("serial")
    if serial is None:
        if schema is not None and schema.serial_names:
            raise ConfigError(f"缺少配置段 'serial'，需要端点: {list(schema.serial_names)}")
        return
    if not isinstance(serial, Mapping):
        raise ConfigError(f"配置项 'serial' 应为映射，实际为 {type(serial).__name__}")
    for name, endpoint in serial.items():
        from_dict(SerialEndpoint, endpoint, f"serial.{name}")
    if schema is None:
        return
    missing = [name for name in schema.serial_names if name not in serial]
    if missing:
        raise ConfigError(f"缺少串口端点 serial.{', serial.'.join(missing)}")


def _validate_relay(data: Mapping[str, Any], schema: AppSchema | None) -> None:
    if "relay" in data and data["relay"] is not None:
        from_dict(RelayConfig, data["relay"], "relay")
    elif schema is not None and schema.relay:
        raise ConfigError("缺少配置段 'relay'")

    relays = data.get("relays")
    if relays is None:
        if schema is not None and schema.relays:
            raise ConfigError("缺少配置段 'relays'")
        return
    if not isinstance(relays, Mapping):
        raise ConfigError(f"配置项 'relays' 应为映射，实际为 {type(relays).__name__}")
    for name, cfg in relays.items():
        from_dict(RelayConfig, cfg, f"relays.{name}")


def _validate_keywords(data: Mapping[str, Any]) -> None:
    if "keywords" in data and data["keywords"] is not None:
        from_dict(KeywordConfig, data["keywords"], "keywords")


@lru_cache(maxsize=1)
def _registry() -> dict[str, AppSchema]:
    """延迟导入各应用的配置 dataclass，避免 settings 与 apps 循环依赖。"""
    from ppx_testkit.apps.assembly.r3_assembly_upgrade import AssemblyConfig
    from ppx_testkit.apps.calculators.speed_calc import SpeedCalcConfig
    from ppx_testkit.apps.calculators.time_diff import TimeDiffConfig
    from ppx_testkit.apps.charge.charge_cycle import ChargeCycleConfig
    from ppx_testkit.apps.fct.p3_fcb import P3FctConfig
    from ppx_testkit.apps.fct.r3_leb import LebFctConfig
    from ppx_testkit.apps.fct.r3_mcb import McbFctConfig
    from ppx_testkit.apps.lamp._common import LampConfig
    from ppx_testkit.apps.motor.brake_lock_unlock import BrakeTestConfig, RegionProtocolConfig
    from ppx_testkit.apps.pctool.common import WindowConfig
    from ppx_testkit.apps.pctool.inspect_controls import InspectConfig
    from ppx_testkit.apps.pctool.l5_upgrade_stress import StepStressConfig
    from ppx_testkit.apps.pctool.mouse_locate import LocateConfig
    from ppx_testkit.apps.pctool.upgrade_log_stress import UpgradeLogConfig
    from ppx_testkit.apps.pctool.w3_pctool_stress import W3StressConfig
    from ppx_testkit.apps.power_cycle.power_button import PowerButtonConfig
    from ppx_testkit.apps.power_cycle.relay_power_on import RelayPowerCycleConfig
    from ppx_testkit.apps.power_cycle.relay_probe import RelayProbeConfig
    from ppx_testkit.apps.servo.servo_nfc import ServoNfcConfig
    from ppx_testkit.apps.whitebox._mcb_common import McbLinkConfig
    from ppx_testkit.apps.whitebox.lcb_ble import BleLinkConfig, LcbBleConfig
    from ppx_testkit.apps.whitebox.lrd_debug import LrdLedConfig
    from ppx_testkit.apps.whitebox.mcb_hall_diag import HallDiagConfig
    from ppx_testkit.apps.whitebox.mcb_region import HeartbeatConfig, McbWhiteboxConfig
    from ppx_testkit.apps.whitebox.r3_mcb import R3McbConfig, R3ProtocolConfig

    lamp = AppSchema(sections=(SectionRule("lamp", LampConfig),), relays=True)
    return {
        "power_cycle.relay_power_on": AppSchema(
            sections=(SectionRule("test", RelayPowerCycleConfig),),
            serial_names=("relay",),
            relay=True,
        ),
        "power_cycle.power_button": AppSchema(
            sections=(SectionRule("test", PowerButtonConfig),),
            serial_names=("relay", "device"),
            relay=True,
        ),
        "power_cycle.relay_probe": AppSchema(
            sections=(SectionRule("test", RelayProbeConfig),),
            serial_names=("relay",),
            relay=True,
        ),
        "charge.charge_cycle": AppSchema(
            sections=(SectionRule("test", ChargeCycleConfig),),
            serial_names=("relay", "device"),
            relay=True,
        ),
        "servo.servo_nfc": AppSchema(
            sections=(SectionRule("test", ServoNfcConfig),),
            serial_names=("servo", "device"),
        ),
        "lamp.headlight": lamp,
        "lamp.turn_signal": lamp,
        "whitebox.lcb_ble": AppSchema(
            sections=(
                SectionRule("link", BleLinkConfig, required=False),
                SectionRule("lcb_ble", LcbBleConfig, required=False),
            ),
            serial_names=("lcb",),
        ),
        "whitebox.lrd_debug": AppSchema(
            sections=(
                SectionRule("link", BleLinkConfig, required=False),
                SectionRule("lrd", LrdLedConfig, required=False),
            ),
            serial_names=("lcb",),
        ),
        "whitebox.mcb_region": AppSchema(
            sections=(
                SectionRule("link", McbLinkConfig, required=False),
                SectionRule("heartbeat", HeartbeatConfig, required=False),
                SectionRule("mcb_whitebox", McbWhiteboxConfig, required=False),
            ),
            serial_names=("mcb",),
        ),
        "whitebox.mcb_hall_diag": AppSchema(
            sections=(
                SectionRule("link", McbLinkConfig, required=False),
                SectionRule("hall_diag", HallDiagConfig, required=False),
            ),
            serial_names=("mcb",),
        ),
        "whitebox.r3_mcb": AppSchema(
            sections=(
                SectionRule("protocol", R3ProtocolConfig),
                SectionRule("r3_mcb", R3McbConfig),
            ),
            serial_names=("mcb",),
        ),
        "motor.brake_lock_unlock": AppSchema(
            sections=(
                SectionRule("protocol", RegionProtocolConfig),
                SectionRule("brake_test", BrakeTestConfig),
            ),
            serial_names=("tested", "load"),
        ),
        "fct.p3_fcb": AppSchema(sections=(SectionRule("fct", P3FctConfig),)),
        "fct.r3_leb": AppSchema(sections=(SectionRule("fct", LebFctConfig),)),
        "fct.r3_mcb": AppSchema(
            sections=(SectionRule("fct", McbFctConfig),),
            serial_names=("relay",),
            relay=True,
        ),
        "assembly.r3_assembly_upgrade": AppSchema(sections=(SectionRule("assembly", AssemblyConfig),)),
        "pctool.l5_upgrade_stress": AppSchema(
            sections=(
                SectionRule("stress", StepStressConfig),
                SectionRule("window", WindowConfig, required=False),
            ),
        ),
        "pctool.w3_pctool_stress": AppSchema(sections=(SectionRule("stress", W3StressConfig),)),
        "pctool.upgrade_log_stress": AppSchema(
            sections=(
                SectionRule("window", WindowConfig),
                SectionRule("stress", UpgradeLogConfig),
            ),
        ),
        "pctool.inspect_controls": AppSchema(
            sections=(
                SectionRule("window", WindowConfig),
                SectionRule("inspect", InspectConfig, required=False),
            ),
        ),
        "pctool.mouse_locate": AppSchema(
            sections=(SectionRule("locate", LocateConfig, required=False),),
        ),
        "calculators.speed_calc": AppSchema(
            sections=(SectionRule("speed_calc", SpeedCalcConfig, required=False),),
        ),
        "calculators.time_diff": AppSchema(
            sections=(SectionRule("time_diff", TimeDiffConfig, required=False),),
        ),
    }
