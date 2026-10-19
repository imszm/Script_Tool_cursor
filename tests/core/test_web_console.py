"""网页运行台：不接硬件，检查工位表单、命令参数和单次运行锁。"""

from __future__ import annotations

import sys
import time

import pytest
import yaml

from ppx_testkit.exceptions import ConfigError
from ppx_testkit.settings import load_settings
from ppx_testkit.web.catalog import (
    build_run_args,
    cycle_field,
    save_local_ports,
    serial_fields,
    station_catalog,
)
from ppx_testkit.web.server import _page_path
from ppx_testkit.web.session import RunSession


def test_catalog_includes_charge_stations() -> None:
    rows = station_catalog()
    names = {row["name"] for row in rows}
    assert {"l5_charge_cycle", "r3_charge_cycle", "relay_power_cycle"} <= names
    relay = next(row for row in rows if row["name"] == "relay_power_cycle")
    assert {item["name"] for item in relay["serial"]} == {"relay", "device"}
    assert relay["cycle_key"] == "test.cycles"
    assert relay["caution"] is None


def test_charge_station_exposes_relay_and_device() -> None:
    settings = load_settings("r3_charge_cycle", use_local=False, environ={})
    data = dict(settings.data)
    names = [item["name"] for item in serial_fields(data)]
    assert names == ["relay", "device"]
    assert cycle_field(data)[0] == "test.cycles"


def test_turn_signal_cycle_field_is_lamp() -> None:
    settings = load_settings("turn_signal", use_local=False, environ={})
    key, default = cycle_field(dict(settings.data))
    assert key == "lamp.cycles"
    assert default is None


def test_build_run_args_ports_and_cycles() -> None:
    args = build_run_args("relay_power_cycle", {"device": "COM13", "relay": "COM3"}, 2, "test.cycles")
    assert args[:5] == [sys.executable, "-m", "ppx_testkit", "run", "relay_power_cycle"]
    assert args[args.index("--port") + 1] == "device=COM13"
    assert "relay=COM3" in args
    assert args[-2:] == ["--set", "test.cycles=2"]


def test_build_run_args_rejects_bad_port() -> None:
    with pytest.raises(ConfigError):
        build_run_args("relay_power_cycle", {"relay": "COM3 & calc"}, None, None)


def test_save_local_ports_merges(project_tmp) -> None:
    path = project_tmp / "config" / "local.yaml"
    path.write_text("logging:\n  root_dir: D:/TestLogs\nstations: {}\n", encoding="utf-8")
    save_local_ports("relay_power_cycle", {"relay": "COM9", "device": ""}, path)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["logging"]["root_dir"] == "D:/TestLogs"
    assert saved["stations"]["relay_power_cycle"]["serial"]["relay"]["match"]["port"] == "COM9"
    assert "device" not in saved["stations"]["relay_power_cycle"]["serial"]


def test_page_file_exists() -> None:
    assert _page_path().is_file()


def test_only_one_run_and_stop() -> None:
    code = (
        "import time\n"
        "print('started', flush=True)\n"
        "try:\n"
        "    time.sleep(30)\n"
        "except KeyboardInterrupt:\n"
        "    raise SystemExit(130)\n"
    )
    session = RunSession()
    args = [sys.executable, "-c", code]
    session.start("demo", args, project_log := _unused_log())
    try:
        with pytest.raises(ConfigError, match="已有测试在运行"):
            session.start("demo", args, project_log)
        seen = False
        for _ in range(50):
            if any("started" in line for line in session.log_from(0)["lines"]):
                seen = True
                break
            time.sleep(0.05)
        assert seen
        session.stop(grace_s=3)
        assert session.running() is False
        assert session.snapshot()["exit_code"] is not None
    finally:
        session.stop(grace_s=1)


def _unused_log():
    from pathlib import Path

    return Path("logs") / "web_console_test"
