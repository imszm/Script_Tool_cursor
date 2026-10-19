"""R3 充电日志按块读取：跨块拼接、数字边界、继电器发令期间不丢关键字。"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from ppx_testkit.apps.charge.charge_cycle import ChargeCycle, ChargeCycleConfig
from ppx_testkit.core.monitor.chunk_matcher import ChunkMatcher
from ppx_testkit.core.monitor.chunk_reader import ChunkLogMonitor
from ppx_testkit.core.monitor.keyword_rules import KeywordConfig, RateRule
from ppx_testkit.core.relay.base import ChannelCommands, RelayConfig
from ppx_testkit.core.relay.drivers import CommandRelay
from ppx_testkit.core.serial.transport import SerialEndpoint
from ppx_testkit.exceptions import TestAbort
from ppx_testkit.settings import load_settings
from ppx_testkit.utils.ansi import AnsiStreamFilter


def _relay(transport: Any) -> CommandRelay:
    cfg = RelayConfig(channels={1: ChannelCommands(on=0x50, off=0x4F)}, post_write_delay_s=0, drain_response=False)
    return CommandRelay(transport, cfg, sleep=lambda _s: None)


def test_success_keyword_survives_split_and_whitespace() -> None:
    matcher = ChunkMatcher(KeywordConfig(success=["voice_msg num: 1"]), digit_boundary=True)
    matcher.reset_cycle()
    assert matcher.feed("voice_msg nu").success is None
    verdict = matcher.feed("m: 1\n")
    assert verdict.success == "voice_msg num: 1"


def test_num_1_does_not_match_num_10() -> None:
    matcher = ChunkMatcher(KeywordConfig(success=["voice_msg num: 1", "voice_msg num: 2"]), digit_boundary=True)
    matcher.reset_cycle()
    assert matcher.feed("voice_msg num: 10\n").success is None
    assert matcher.feed("voice_msg num: 2\n").success == "voice_msg num: 2"


def test_rate_rule_aborts_inside_one_chunk() -> None:
    matcher = ChunkMatcher(
        KeywordConfig(rate_rules=[RateRule("paramisinvalid", 3, 3, keep_power=False)]),
        digit_boundary=True,
    )
    text = " paramisinvalid " * 3
    verdict = matcher.feed(text)
    assert verdict.abort_reason
    assert verdict.abort_keep_power is False


def test_ansi_sequence_split_across_chunks() -> None:
    filt = AnsiStreamFilter()
    assert "\x1b" not in filt.feed("ok\x1b[31")
    assert filt.feed("m voice\x1b[0m").startswith(" voice")


def test_rx_buffer_falls_back_to_smaller_size(fake_factory, make_transport) -> None:
    seen: list[int] = []

    def setup(handle: Any) -> None:
        def set_buffer_size(*, rx_size: int, tx_size: int) -> None:
            seen.append(rx_size)
            if rx_size > 1000:
                raise OSError("too big")

        handle.set_buffer_size = set_buffer_size

    fake_factory.setup = setup
    make_transport("DEV", rx_buffer_sizes=(5000, 1000))
    assert seen == [5000, 1000]


def test_l5_keyword_9_does_not_match_90() -> None:
    keywords = KeywordConfig(success=["voice_msgnum:9", "voice_msgnum:10"])

    def once(text: str) -> str | None:
        matcher = ChunkMatcher(keywords, digit_boundary=True)
        matcher.reset_cycle()
        return matcher.feed(text).success

    assert once("voice_msgnum:90\n") is None
    assert once("voice_msgnum:100\n") is None
    assert once("voice_msg num: 9\n") == "voice_msgnum:9"
    assert once("voice_msgnum:10\n") == "voice_msgnum:10"


def test_charge_stations_use_chunk_listen() -> None:
    for name in ("r3_charge_cycle", "l5_charge_cycle"):
        settings = load_settings(name, use_local=False, environ={})
        cfg = settings.section("test", ChargeCycleConfig)
        device = settings.section("serial.device", SerialEndpoint)
        assert cfg.listen == "chunk", name
        assert cfg.success_digit_boundary is True
        assert device.timeout == 0
        assert device.rx_buffer_sizes[0] == 1048576


def _chunk_cycle(make_transport, keywords: KeywordConfig, **cfg: Any) -> tuple[ChargeCycle, ChunkLogMonitor, Any]:
    relay_t = make_transport("RELAY")
    dev_t = make_transport("DEV", timeout=0, log_traffic=False)
    mon = ChunkLogMonitor(
        dev_t, keywords, digit_boundary=True, read_idle_s=0.001, drain_grace_s=0.02, reconnect_delay_s=0.05,
    )
    base: dict[str, Any] = dict(
        cycles=1, charge_on_s=0.6, post_success_hold_s=0, off_reset_s=0,
        initial_off_s=None, notify=False, listen="chunk", success_digit_boundary=True,
    )
    base.update(cfg)
    cycle = ChargeCycle(
        ChargeCycleConfig(**base),
        _relay(relay_t),
        mon,
        sleep=lambda _s: None,
    )
    return cycle, mon, dev_t


def test_keyword_arriving_during_relay_on_is_counted(make_transport, fake_factory) -> None:
    cycle, mon, _dev = _chunk_cycle(make_transport, KeywordConfig(success=["voice_msg num: 1"]))
    device = next(h for h in fake_factory.opened if h.port == "DEV")
    original = cycle.relay.on

    def on(channel: int = 1) -> None:
        device.feed("voice_msg nu")
        time.sleep(0.05)
        device.feed("m: 1\n")
        time.sleep(0.08)
        original(channel)

    cycle.relay.on = on  # type: ignore[method-assign]
    cycle.setup()
    try:
        result = cycle.run_cycle(1)
    finally:
        mon.stop()
    assert result.passed
    assert "voice_msg num: 1" in result.detail


def test_chunk_rate_abort_stops_the_run(make_transport, fake_factory) -> None:
    cycle, mon, _dev = _chunk_cycle(
        make_transport,
        KeywordConfig(
            success=["voice_msg num: 1"],
            rate_rules=[RateRule("paramisinvalid", 3, 3, keep_power=False)],
        ),
        charge_on_s=0.8,
    )
    device = next(h for h in fake_factory.opened if h.port == "DEV")
    original = cycle.relay.on

    def on(channel: int = 1) -> None:
        device.feed("paramisinvalid\n" * 3)
        time.sleep(0.08)
        original(channel)

    cycle.relay.on = on  # type: ignore[method-assign]
    cycle.setup()
    try:
        with pytest.raises(TestAbort) as exc:
            cycle.run_cycle(1)
    finally:
        mon.stop()
    assert exc.value.keep_power is False


def test_reader_stops_cleanly() -> None:
    """没有设备数据时，读线程也能在时限内退出。"""
    from ppx_testkit.core.serial.transport import SerialTransport

    class _Quiet:
        is_open = True
        in_waiting = 0

        def read(self, _n: int) -> bytes:
            return b""

        def close(self) -> None:
            self.is_open = False

        def reset_input_buffer(self) -> None:
            return None

        def reset_output_buffer(self) -> None:
            return None

    transport = SerialTransport("QUIET", 115200, timeout=0, log_traffic=False, factory=lambda *a, **k: _Quiet())
    transport.open()
    mon = ChunkLogMonitor(transport, KeywordConfig(success=["voice_msg num: 1"]), read_idle_s=0.01)
    mon.start()
    done = threading.Event()

    def _stop() -> None:
        mon.stop(timeout=2)
        done.set()

    threading.Thread(target=_stop).start()
    assert done.wait(3)
    transport.close()
