"""车速计算器 / 时间差计算器单元测试（纯函数 + cli 模式；GUI 只测缺少 tkinter 时的降级）。"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

import pytest

from ppx_testkit.apps.calculators import speed_calc, time_diff
from ppx_testkit.apps.calculators.speed_calc import (
    SpeedCalcConfig,
    compute_speed,
    evaluate_text_inputs,
    format_result,
)
from ppx_testkit.apps.calculators.time_diff import (
    TimeDiffConfig,
    format_hours,
    parse_time,
    time_diff_hours,
)
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import from_dict, load_settings


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(station="t", run_id="r", run_dir=tmp_path, started_at=_dt.datetime.now())


# ============================================================ 车速
class TestSpeedCalc:
    def test_default_parameters(self) -> None:
        r = compute_speed(3728, 6.2, 8)
        assert r.wheel_rpm == pytest.approx(601.2903226)
        assert r.circumference_m == pytest.approx(0.6383716272)
        assert r.speed_kmh == pytest.approx(23.0308009)
        assert r.speed_mph == pytest.approx(14.3106762)
        assert format_result(r) == "计算结果: \n23.03 km/h\n14.31 mp/h"

    def test_zero_and_negative_rpm(self) -> None:
        assert compute_speed(0, 6.2, 8).speed_kmh == 0
        assert compute_speed(-3728, 6.2, 8).speed_kmh == pytest.approx(-23.0308009)

    @pytest.mark.parametrize("ratio,dia", [(0, 8), (-1, 8), (6.2, 0), (6.2, -3), (float("nan"), 8)])
    def test_invalid_ratio_or_diameter(self, ratio: float, dia: float) -> None:
        with pytest.raises(ValueError, match="参数有误"):
            compute_speed(3728, ratio, dia)

    def test_text_inputs_like_legacy_gui(self) -> None:
        assert evaluate_text_inputs("", "6.2", "8") == ("等待输入完整参数...", None)
        assert evaluate_text_inputs("37a", "6.2", "8") == ("输入格式不合法...", None)
        assert evaluate_text_inputs("3728", "0", "8") == ("参数有误: 减速比和外径需大于 0", None)
        text, result = evaluate_text_inputs(" 3728 ", "6.2", "8")
        assert text.endswith("23.03 km/h\n14.31 mp/h") and result is not None

    def test_config_validation(self) -> None:
        with pytest.raises(ConfigError, match="减速比"):
            from_dict(SpeedCalcConfig, {"gear_ratio": 0})
        with pytest.raises(ConfigError, match="用例"):
            from_dict(SpeedCalcConfig, {"cases": [{"rpm": 1, "gear_ratio": 1, "wheel_diameter_inch": 0}]})
        with pytest.raises(ConfigError):
            from_dict(SpeedCalcConfig, {"mode": "web"})

    def test_cli_run_writes_results(self, settings_factory, tmp_path) -> None:
        settings = settings_factory({"speed_calc": {
            "mode": "cli",
            "cases": [{"name": "10寸", "rpm": 3728, "gear_ratio": 6.2, "wheel_diameter_inch": 10}],
        }})
        assert speed_calc.run(settings, _ctx(tmp_path)) == 0
        rows = json.loads((tmp_path / "speed_calc.json").read_text(encoding="utf-8"))
        assert [r["name"] for r in rows] == ["默认参数", "10寸"]
        assert rows[0]["speed_kmh"] == pytest.approx(23.0308, abs=1e-4)
        assert rows[1]["speed_kmh"] == pytest.approx(23.0308009 * 10 / 8, abs=1e-4)
        assert (tmp_path / "speed_calc.csv").exists()

    def test_gui_without_tkinter_returns_fail(self, settings_factory, tmp_path, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "tkinter", None)
        assert speed_calc.run(settings_factory({"speed_calc": {"mode": "gui"}}), _ctx(tmp_path)) == 1


# ============================================================ 时间差
class TestTimeDiff:
    def test_legacy_example(self) -> None:
        assert time_diff_hours("2026/4/8 16:45:28", "2026/4/9 2:49:39") == pytest.approx(10.0697222)
        assert format_hours(10.0697222) == "时间差: 10.0697 小时"

    def test_negative_and_zero(self) -> None:
        assert time_diff_hours("2026/4/9 2:00:00", "2026/4/9 1:00:00") == pytest.approx(-1.0)
        assert time_diff_hours("2026/4/9 2:00:00", "2026/4/9 2:00:00") == 0

    def test_bad_format_message(self) -> None:
        with pytest.raises(ValueError, match="2026/4/8 16:45:28"):
            time_diff_hours("2026-04-08", "2026/4/9 2:49:39")
        with pytest.raises(ValueError, match="不能为空"):
            parse_time("  ")

    def test_multiple_formats(self) -> None:
        fmts = ["%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"]
        assert time_diff_hours("2026-04-08 16:00:00", "2026/4/8 17:30:00", fmts) == pytest.approx(1.5)

    def test_config_validation(self) -> None:
        with pytest.raises(ConfigError, match="同时配置"):
            from_dict(TimeDiffConfig, {"start": "2026/4/8 16:45:28"})
        with pytest.raises(ConfigError, match="同时配置"):
            from_dict(TimeDiffConfig, {"start": "", "end": "2026/4/8 16:45:28"})
        with pytest.raises(ConfigError, match="第 1 组 end"):
            from_dict(TimeDiffConfig, {"start": "2026/4/8 16:45:28", "end": "bad"})
        with pytest.raises(ConfigError, match="formats"):
            from_dict(TimeDiffConfig, {"formats": []})

    def test_cli_run(self, settings_factory, tmp_path) -> None:
        settings = settings_factory({"time_diff": {
            "mode": "cli",
            "start": "2026/4/8 16:45:28",
            "end": "2026/4/9 2:49:39",
            "pairs": [{"name": "一小时", "start": "2026/1/1 0:00:00", "end": "2026/1/1 1:00:00"}],
        }})
        assert time_diff.run(settings, _ctx(tmp_path)) == 0
        rows = json.loads((tmp_path / "time_diff.json").read_text(encoding="utf-8"))
        assert [r["hours"] for r in rows] == [10.0697, 1.0]
        assert rows[0]["seconds"] == 36251.0

    def test_cli_without_pairs_is_config_error(self, settings_factory, tmp_path) -> None:
        with pytest.raises(ConfigError, match="cli 模式"):
            time_diff.run(settings_factory({"time_diff": {"mode": "cli"}}), _ctx(tmp_path))

    def test_gui_without_tkinter_returns_fail(self, settings_factory, tmp_path, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "tkinter", None)
        assert time_diff.run(settings_factory({}), _ctx(tmp_path)) == 1


# ============================================================ 工位配置
@pytest.mark.parametrize(
    "station,section,cls",
    [("speed_calc", "speed_calc", SpeedCalcConfig), ("time_diff", "time_diff", TimeDiffConfig)],
)
def test_station_yaml_loads(station: str, section: str, cls: type) -> None:
    settings = load_settings(station, use_local=False, environ={})
    assert settings.app == f"calculators.{station}"
    cfg = settings.section(section, cls)
    assert cfg.mode == "gui"


def test_station_cli_override(tmp_path) -> None:
    settings = load_settings(
        "time_diff",
        overrides={"time_diff": {"mode": "cli", "start": "2026/4/8 16:45:28", "end": "2026/4/9 2:49:39"}},
        use_local=False,
        environ={},
    )
    assert time_diff.run(settings, _ctx(tmp_path)) == 0
