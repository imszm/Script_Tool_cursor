"""PPX PCTOOL 扫码压力：不碰真实键鼠，锁住点击、回车顺序和窗口跟随。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ppx_testkit.apps.pctool.ppx_scan_stress import (
    PpxScanStressConfig,
    PpxScanStressCycle,
    track_point,
)
from ppx_testkit.core.runner import StressRunner
from ppx_testkit.exceptions import ConfigError, WindowNotFoundError
from ppx_testkit.settings import from_dict, load_settings

VIN = "P30101RZKUS6900007"
BARCODES = (
    ("左力矩", "WL680004909715-05"),
    ("右力矩", "WR680003207269+27"),
    ("左电机", "PX01-8120Y064100028"),
    ("右电机", "PX01-8120Y063500294"),
    ("电池", "AV06002LH201F9030150"),
)


def _cfg(**overrides: object) -> PpxScanStressConfig:
    data: dict[str, object] = {
        "vin": VIN,
        "barcodes": [{"label": label, "text": text} for label, text in BARCODES],
        "vin_click": [500, 200],
        "confirm_click": [700, 450],
        "startup_delay_s": 0,
        "after_click_s": 0.2,
        "popup_wait_s": 2,
        "after_enter_s": 0.4,
        "settle_wait_s": 20,
        "confirm_by_name": True,
    }
    data.update(overrides)
    return from_dict(PpxScanStressConfig, data, "stress")


class FakeWindows:
    def __init__(self) -> None:
        self.main: tuple[int, int, int, int] | None = (100, 80, 900, 700)
        self.dialog: tuple[int, int, int, int] | None = (300, 200, 700, 560)
        self.torque: tuple[int, int, int, int] | None = (600, 500, 980, 760)
        self.activated: list[str] = []
        self.visible_titles: list[str] = []

    def rect(self, title: str, *, exact: bool = False) -> tuple[int, int, int, int] | None:
        if title == "扫码":
            return self.dialog
        if title == "力矩校验":
            return self.torque
        return self.main

    def activate(self, title: str) -> bool:
        self.activated.append(title)
        return True

    def titles(self) -> list[str]:
        return list(self.visible_titles)


class ShiftOnActivate(FakeWindows):
    """模拟从任务栏拉起后窗口位置变化。点击时读到的必须是拉起之后的位置。"""

    def activate(self, title: str) -> bool:
        self.activated.append(title)
        if self.main is not None:
            left, top, right, bottom = self.main
            self.main = (left + 50, top + 10, right + 50, bottom + 10)
        return True


class FakeScreen:
    def __init__(self, windows: FakeWindows | None = None) -> None:
        self.windows = windows
        self.clicks: list[tuple[tuple[int, int], str]] = []
        self.click_origins: list[tuple[int, int] | None] = []
        self.texts: list[str] = []
        self.enters: list[bool] = []
        self.literals: list[bool] = []
        self.hotkeys: list[tuple[str, ...]] = []
        self.presses: list[str] = []
        self.clears: list[bool] = []
        self.shots: list[Path] = []

    def click(self, point: tuple[int, int], *, label: str = "") -> None:
        self.clicks.append((point, label))
        origin = None if self.windows is None or self.windows.main is None else self.windows.main[:2]
        self.click_origins.append(origin)

    def hotkey(self, *keys: str) -> None:
        self.hotkeys.append(keys)

    def press(self, key: str) -> None:
        self.presses.append(key)

    def type_text(self, text: str, *, interval: float = 0.02, enter: bool = False) -> None:
        self.texts.append(text)
        self.enters.append(enter)

    def replace_text(
        self,
        text: str,
        *,
        enter: bool = False,
        clear: bool = True,
        literal: bool = False,
        interval: float = 0.03,
    ) -> None:
        self.texts.append(text)
        self.enters.append(enter)
        self.clears.append(clear)
        self.literals.append(literal)

    def save_screenshot(self, path: Path) -> Path | None:
        self.shots.append(path)
        return path


class FakeButtons:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str, float]] = []
        self.focuses: list[tuple[str, int]] = []

    def click_button(self, title_re: str, button: str, timeout_s: float) -> bool:
        self.calls.append((title_re, button, timeout_s))
        return self.ok

    def focus_edit(self, title_re: str, index: int) -> bool:
        self.focuses.append((title_re, index))
        return False


def _cycle(
    cfg: PpxScanStressConfig | None = None,
    *,
    named_ok: bool = True,
    windows: FakeWindows | None = None,
) -> tuple[PpxScanStressCycle, FakeScreen, FakeWindows, FakeButtons, list[float]]:
    wins = windows or FakeWindows()
    screen = FakeScreen(wins)
    buttons = FakeButtons(named_ok)
    sleeps: list[float] = []
    cycle = PpxScanStressCycle(cfg or _cfg(), screen, wins, buttons, sleep=sleeps.append)
    return cycle, screen, wins, buttons, sleeps


def test_track_point_locks_then_follows() -> None:
    first, origin = track_point((500, 200), (100, 80), None)
    assert first == (500, 200) and origin == (100, 80)
    second, origin2 = track_point((500, 200), (130, 90), origin)
    assert second == (530, 210) and origin2 == (100, 80)


def test_station_yaml_keeps_vin_and_barcode_order() -> None:
    settings = load_settings("ppx_pctool_stress", use_local=False, environ={})
    cfg = settings.section("stress", PpxScanStressConfig)
    assert cfg.vin == "P30101RZKUS6900002"
    assert [(item.label, item.text) for item in cfg.barcodes] == [
        ("左力矩", "WL680004409882+16"),
        ("右力矩", "WR680004408644+01"),
        ("左电机", "PX01-8120Y063203608"),
        ("右电机", "PX01-8120Y064000335"),
        ("电池", "AV06002LH201F9030111"),
    ]
    assert cfg.popup_wait_s == 2
    assert cfg.settle_wait_s == 20
    assert cfg.torque_title == "力矩校验"
    assert cfg.torque_confirm_click == (874, 681)
    assert cfg.after_torque_click_s == 10
    assert cfg.cycles == 100


def test_leading_space_is_removed_before_paste() -> None:
    spaced = [{"label": label, "text": f" {text} "} for label, text in BARCODES]
    cycle, screen, _, _, _ = _cycle(_cfg(vin=f" {VIN} ", barcodes=spaced))
    cycle.run_cycle(1)
    assert screen.texts[0] == VIN
    assert screen.texts[1:] == [text for _, text in BARCODES]


def test_config_rejects_empty_flow() -> None:
    with pytest.raises(ConfigError):
        _cfg(barcodes=[])
    with pytest.raises(ConfigError):
        _cfg(cycles=0)
    with pytest.raises(ConfigError):
        _cfg(popup_wait_s=-1)


def test_one_cycle_types_vin_then_each_barcode_with_enter() -> None:
    cycle, screen, windows, buttons, sleeps = _cycle(named_ok=True)
    result = cycle.run_cycle(1)
    assert result.passed
    assert windows.activated == ["PPX PCTOOL"]
    assert screen.clicks == [((500, 200), "VIN输入框")]
    assert screen.clears == [True, False, False, False, False, False]
    assert screen.texts == [VIN, *[text for _, text in BARCODES]]
    assert screen.enters == [True] * 6
    assert screen.literals == [False, True, True, True, True, True]
    assert buttons.focuses == []
    assert [call[1] for call in buttons.calls] == ["确认", "确认"]
    assert "扫码" in buttons.calls[0][0]
    assert "力矩校验" in buttons.calls[1][0]
    assert sleeps == [0.2, 2, 0.4, 0.4, 0.4, 0.4, 0.4, 20, 10]


def test_confirm_falls_back_to_coordinate_and_follows_dialog() -> None:
    windows = FakeWindows()
    cycle, screen, _, _, _ = _cycle(named_ok=False, windows=windows)
    cycle.run_cycle(1)
    assert screen.clicks[1] == ((700, 450), "确认")
    assert screen.clicks[2] == ((868, 680), "力矩校验确认")
    windows.main = (130, 90, 930, 710)
    windows.dialog = (305, 206, 705, 566)
    windows.torque = (620, 510, 1000, 770)
    cycle.run_cycle(2)
    assert screen.clicks[3] == ((530, 210), "VIN输入框")
    assert screen.clicks[4] == ((705, 456), "确认")
    assert screen.clicks[5] == ((888, 690), "力矩校验确认")


def test_absolute_mode_ignores_window_move() -> None:
    windows = FakeWindows()
    cycle, screen, _, _, _ = _cycle(_cfg(follow_window=False), named_ok=False, windows=windows)
    cycle.run_cycle(1)
    windows.main = (400, 400, 900, 700)
    windows.dialog = (10, 10, 20, 20)
    cycle.run_cycle(2)
    assert screen.clicks[3][0] == (500, 200)
    assert screen.clicks[4][0] == (700, 450)
    assert screen.clicks[2][0] == (868, 680)
    assert screen.clicks[5][0] == (868, 680)


def test_missing_dialog_aborts_before_barcodes() -> None:
    windows = FakeWindows()
    windows.dialog = None
    windows.visible_titles = ["PPX PCTOOL V2.5.00", "Cursor"]
    cycle, screen, _, _, _ = _cycle(windows=windows)
    result = cycle.run_cycle(1)
    assert not result.passed
    assert "扫码" in result.detail
    assert "PPX PCTOOL V2.5.00" in result.detail
    assert screen.texts == [VIN]
    assert len(screen.clicks) == 1
    assert cycle.rows[-1]["verdict"] == "FAIL"


def test_vin_click_uses_position_after_activate() -> None:
    windows = ShiftOnActivate()
    cycle, screen, _, _, _ = _cycle(named_ok=False, windows=windows)
    cycle.run_cycle(1)
    assert screen.click_origins[0] == (150, 90)
    assert screen.clicks[0][0] == (500, 200)
    cycle.run_cycle(2)
    assert screen.click_origins[3] == (200, 100)
    assert screen.clicks[3][0] == (550, 210)


def test_missing_torque_dialog_aborts_after_scan_confirm() -> None:
    windows = FakeWindows()
    windows.torque = None
    cycle, screen, _, _, _ = _cycle(named_ok=True, windows=windows)
    result = cycle.run_cycle(1)
    assert not result.passed and "力矩校验" in result.detail
    assert screen.texts == [VIN, *[text for _, text in BARCODES]]
    assert screen.clicks == [((500, 200), "VIN输入框")]


def test_missing_dialog_counts_as_failed_cycle_and_stops() -> None:
    windows = FakeWindows()
    windows.dialog = None
    cycle, _, _, _, _ = _cycle(windows=windows)
    summary = StressRunner(cycle, 2, station="ppx_pctool_stress", stop_on_fail=True).run()
    assert summary.aborted
    assert (summary.executed, summary.passed, summary.failed) == (1, 0, 1)
    assert summary.pass_rate == 0.0
    assert "扫码" in (summary.abort_reason or "")


def test_missing_main_window_aborts_in_cycle_and_setup() -> None:
    windows = FakeWindows()
    windows.main = None
    cycle, screen, _, _, _ = _cycle(windows=windows)
    result = cycle.run_cycle(1)
    assert not result.passed and "PPX PCTOOL" in result.detail
    assert screen.clicks == []
    with pytest.raises(WindowNotFoundError):
        cycle.setup()
