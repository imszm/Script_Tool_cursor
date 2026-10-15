from __future__ import annotations

import pytest

from ppx_testkit.core.monitor import KeywordConfig, KeywordEvaluator, RateRule, SlidingWindowCounter
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.utils.ansi import clean_line, normalize, strip_ansi
from ppx_testkit.utils.sn import increment_serial, serial_batch, serial_sequence


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------- ansi
def test_ansi_helpers() -> None:
    raw = "\x1b[32mVoice_Msg Num: 1\x1b[0m\r\n"
    assert strip_ansi(raw) == "Voice_Msg Num: 1\r\n"
    assert clean_line(raw) == "Voice_Msg Num: 1"
    assert normalize(raw) == "voice_msgnum:1"


# ---------------------------------------------------------------- rate limiter
def test_sliding_window_counter() -> None:
    clk = Clock()
    c = SlidingWindowCounter(10, 3, clk)
    assert not c.hit()
    clk.t = 5
    assert not c.hit()
    clk.t = 11  # 第一次命中(t=0)已滑出窗口
    assert not c.hit()
    assert c.count() == 2
    clk.t = 12
    assert c.hit()
    c.reset()
    assert c.count() == 0


def test_sliding_window_counter_validation() -> None:
    with pytest.raises(ValueError):
        SlidingWindowCounter(0, 1)
    with pytest.raises(ValueError):
        SlidingWindowCounter(1, 0)


# ---------------------------------------------------------------- keyword rules
def test_normalized_matching_is_case_and_space_insensitive() -> None:
    ev = KeywordEvaluator(KeywordConfig(success=["voice_msgnum:9"], exception=["HardFault"], info=["boot"]))
    v = ev.feed("\x1b[33m[BOOT] Voice_Msg Num: 9\x1b[0m")
    assert v.success == "voice_msgnum:9" and v.success_source == "line"
    assert v.infos == ["boot"] and v.exceptions == []
    v2 = ev.feed("hard fault at 0x0800")
    assert v2.exceptions == ["HardFault"] and ev.exception_count == 1


def test_exact_mode_is_case_sensitive() -> None:
    ev = KeywordEvaluator(KeywordConfig(success=["Power On"], match_mode="exact", concat_buffer_max=0))
    assert ev.feed("power on").success is None
    assert ev.feed("xx Power On yy").success == "Power On"


def test_success_split_across_lines_found_in_buffer() -> None:
    ev = KeywordEvaluator(KeywordConfig(success=["voice_msg num: 1"]))
    assert ev.feed("voice_ms").success is None
    v = ev.feed("g num: 1")
    assert v.success == "voice_msg num: 1" and v.success_source == "buffer"
    assert ev.cycle_success == "voice_msg num: 1"
    # 本轮已成功，后续行不再重复判定
    assert ev.feed("voice_msg num: 1").success is None
    ev.reset_cycle()
    assert ev.cycle_success is None
    assert ev.feed("voice_msg num: 1").success_source == "line"


def test_concat_buffer_bounded() -> None:
    ev = KeywordEvaluator(KeywordConfig(success=["abcdef"], concat_buffer_max=4))
    ev.feed("abc")
    assert ev.feed("def").success is None


def test_abort_keyword() -> None:
    ev = KeywordEvaluator(KeywordConfig(abort=["Watchdog Reset"], abort_keep_power=False))
    v = ev.feed("!!! watchdog reset !!!")
    assert v.should_abort and "Watchdog Reset" in (v.abort_reason or "") and v.abort_keep_power is False


def test_rate_rule_triggers_abort() -> None:
    clk = Clock()
    cfg = KeywordConfig(rate_rules=[RateRule("nfc error", window_s=10, count=3, severity="critical", keep_power=False)])
    ev = KeywordEvaluator(cfg, clock=clk)
    assert not ev.feed("NFC Error").should_abort
    clk.t = 1
    assert not ev.feed("NFC Error").should_abort
    clk.t = 2
    v = ev.feed("NFC Error")
    assert v.should_abort and "致命" in (v.abort_reason or "") and v.abort_keep_power is False
    ev.reset_rates()
    clk.t = 3
    assert not ev.feed("NFC Error").should_abort


def test_rate_counts_persist_across_cycles() -> None:
    clk = Clock()
    ev = KeywordEvaluator(KeywordConfig(rate_rules=[RateRule("err", 60, 2)]), clock=clk)
    ev.feed("err")
    ev.reset_cycle()
    assert ev.feed("err").should_abort


def test_empty_line_ignored() -> None:
    ev = KeywordEvaluator(KeywordConfig(success=["x"]))
    v = ev.feed("   \r\n")
    assert v.success is None and not v.should_abort


@pytest.mark.parametrize(
    "kwargs",
    [
        {"success": ["  "]},
        {"concat_buffer_max": -1},
    ],
)
def test_keyword_config_validation(kwargs: dict) -> None:
    with pytest.raises(ConfigError):
        KeywordConfig(**kwargs)


def test_rate_rule_validation() -> None:
    with pytest.raises(ConfigError):
        RateRule("x", 0, 1)
    with pytest.raises(ConfigError):
        RateRule(" ", 1, 1)


# ---------------------------------------------------------------- serial numbers
def test_increment_serial() -> None:
    assert increment_serial("2022005002R000GD006400001") == "2022005002R000GD006400002"
    assert increment_serial("SN099") == "SN100"
    assert increment_serial("SN999") == "SN1000"
    assert increment_serial("A10", step=-1) == "A09"
    with pytest.raises(ValueError):
        increment_serial("NODIGITS")
    with pytest.raises(ValueError):
        increment_serial("A0", step=-1)


def test_serial_sequence_and_batch() -> None:
    assert list(serial_sequence("VIN", 8, 10, 3)) == ["VIN008", "VIN009", "VIN010"]
    with pytest.raises(ValueError):
        list(serial_sequence("V", 2, 1, 3))
    assert serial_batch("X01", 3) == ["X01", "X02", "X03"]
    assert serial_batch("X01", 0) == []
