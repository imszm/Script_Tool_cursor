"""机型 profile 注册，以及 region 会话是否按 profile 选择协议版本。"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest

from ppx_testkit.apps.pctool.w3_pctool_stress import W3StressConfig
from ppx_testkit.apps.whitebox._mcb_common import McbLinkConfig, mcb_shadow_regs
from ppx_testkit.apps.whitebox.r3_mcb import R3ProtocolConfig
from ppx_testkit.core.factory import open_profile_region
from ppx_testkit.core.protocol.ppx_types import RegV1
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.profiles import L5, R3, W3, get_model
from ppx_testkit.profiles.l5 import mcb_shadow_regs as l5_shadow_regs


def test_models_are_registered() -> None:
    assert get_model("l5") is L5
    assert get_model("R3") is R3
    assert get_model(" w3 ") is W3
    assert L5.version == "v1"
    assert R3.version == "v2"
    assert W3.model == "w3"
    assert W3.pctool.window_title == "W3 PCTOOL V5.4.00"
    assert W3.pctool.process_keyword == "EW01_PCTOOL"
    with pytest.raises(ConfigError, match="未知机型"):
        get_model("p3")


def test_w3_process_keyword_default_comes_from_profile() -> None:
    assert W3StressConfig.__dataclass_fields__["process_keyword"].default == W3.pctool.process_keyword


def test_l5_link_and_shadow_regs_come_from_profile() -> None:
    link = McbLinkConfig()
    assert link.dll == L5.dll_file("region")
    assert link.dev_id == L5.dev_id
    assert link.rx_timeout_s == L5.rx_timeout_s
    assert link.retries == L5.retries
    assert [(r.reg, r.field, r.skip_when_zero) for r in mcb_shadow_regs()] == [
        (RegV1.RUN_MODE, "run_mode", True),
        (RegV1.RT_SETTING, "rt_setting", False),
        (RegV1.TARGET_SPEED, "target_speed", False),
        (RegV1.DAT_SETTING, "dat_setting", True),
    ]
    assert [(r.reg, r.field, r.skip_when_zero) for r in mcb_shadow_regs()] == [
        (r.reg, r.field, r.skip_when_zero) for r in l5_shadow_regs()
    ]


def test_r3_protocol_keeps_station_retry_defaults() -> None:
    cfg = R3ProtocolConfig(dll_path=R3.dll_file("region"))
    assert cfg.dev_id == R3.dev_id
    assert cfg.retries == 2
    assert cfg.retry_delay_s == 0.05


def test_open_profile_region_uses_profile_version(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    @contextlib.contextmanager
    def fake(settings: object, **kwargs: object) -> Any:
        seen["settings"] = settings
        seen.update(kwargs)
        yield "client"

    monkeypatch.setattr("ppx_testkit.core.factory.region_client_from_settings", fake)
    settings = object()
    dll = Path("custom.dll")
    with open_profile_region(
        settings,  # type: ignore[arg-type]
        R3,
        serial_name="mcb",
        dll=dll,
        dev_id=0x21,
        rx_timeout_s=0.2,
        retries=2,
        retry_delay_s=0.05,
    ) as client:
        assert client == "client"
    assert seen["version"] == "v2"
    assert seen["dll"] == dll
    assert seen["dev_id"] == 0x21
    assert seen["retries"] == 2

    with open_profile_region(settings, L5, serial_name="mcb"):  # type: ignore[arg-type]
        pass
    assert seen["version"] == "v1"
    assert seen["dll"] == L5.dll_file("region")
    assert seen["dev_id"] == L5.dev_id
    assert seen["retries"] == L5.retries
