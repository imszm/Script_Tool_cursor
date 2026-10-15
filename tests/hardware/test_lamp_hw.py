"""灯光压测硬件冒烟测试（替代旧 Tool/TestTurnSignals、Tool/TestHeadlights 下的 pytest 脚本）。

默认跳过；连接继电器后手动执行::

    pytest -m hardware tests/hardware/test_lamp_hw.py

串口在 config/local.yaml 中按工位配置（如 ``stations.turn_signal.serial.relay_switch.match.port``）。
可选环境变量：

* ``PPX_HW_LAMP_CYCLES``：每个工位执行的循环次数（默认 2）；
* ``PPX_HW_TURN_SIGNAL_VARIANT`` / ``PPX_HW_HEADLIGHT_VARIANT``：选择方案（默认用 YAML 中的 variant）。
"""

from __future__ import annotations

import datetime as _dt
import os

import pytest

from ppx_testkit.apps.lamp import headlight, turn_signal
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import load_settings

pytestmark = pytest.mark.hardware

APPS = {"turn_signal": turn_signal, "headlight": headlight}


@pytest.mark.parametrize("station", sorted(APPS))
def test_lamp_station_short_run(station: str, tmp_path) -> None:
    lamp: dict[str, object] = {"cycles": int(os.environ.get("PPX_HW_LAMP_CYCLES", "2"))}
    variant = os.environ.get(f"PPX_HW_{station.upper()}_VARIANT")
    if variant:
        lamp["variant"] = variant
    settings = load_settings(station, overrides={"lamp": lamp})
    ctx = RunContext(station=station, run_id="hw", run_dir=tmp_path, started_at=_dt.datetime.now())

    code = APPS[station].run(settings, ctx)

    report = (tmp_path / "lamp_cycles.csv").read_text(encoding="utf-8-sig") if code else ""
    assert code == 0, f"{station} 硬件冒烟失败，逐轮结果:\n{report}"
