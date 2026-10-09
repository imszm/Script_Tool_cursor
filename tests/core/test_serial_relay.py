from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest
import serial

from conftest import FakeSerialFactory
from ppx_testkit.core.monitor import KeywordConfig, KeywordEvaluator
from ppx_testkit.core.monitor.line_reader import BackgroundLogReader, DeviceLogMonitor
from ppx_testkit.core.relay import (
    CommandRelay,
    IcseA0Relay,
    RelayConfig,
    ServoDriver,
    build_relay,
    icse_a0_frame,
    servo_command,
)
from ppx_testkit.core.relay.base import ChannelCommands
from ppx_testkit.core.serial.port_finder import PortFinder, PortMatcher
from ppx_testkit.core.serial.transport import SerialEndpoint, SerialTransport, open_endpoint
from ppx_testkit.exceptions import (
    ConfigError,
    PortNotFoundError,
    RelayError,
    SerialDisconnectedError,
    SerialOpenError,
    SerialTimeoutError,
)


@dataclass
class PortInfo:
    device: str
    description: str = ""
    hwid: str = ""
    vid: int | None = None
    pid: int | None = None


# ================================================================ PortFinder
PORTS = [
    PortInfo("COM9", "Silicon Labs CP210x USB to UART Bridge", "USB VID:PID=10C4:EA60", 0x10C4, 0xEA60),
    PortInfo("COM3", "USB-SERIAL CH340", "USB VID:PID=1A86:7523", 0x1A86, 0x7523),
    PortInfo("COM4", "USB-SERIAL CH340", "USB VID:PID=1A86:7523", 0x1A86, 0x7523),
]


def test_port_finder_explicit_wins() -> None:
    finder = PortFinder(lambda: [])
    assert finder.find(PortMatcher(port="COM77", description_contains=["x"])) == "COM77"


def test_port_finder_rules_and_exclude(caplog: pytest.LogCaptureFixture) -> None:
    finder = PortFinder(lambda: PORTS)
    assert finder.find(PortMatcher(description_contains=["cp210"])) == "COM9"
    with caplog.at_level("WARNING"):
        assert finder.find(PortMatcher(vid=0x1A86), name="relay") == "COM3"
    assert "多个候选" in caplog.text
    assert finder.find(PortMatcher(hwid_contains=["1a86"]), exclude=["com3"]) == "COM4"
    assert finder.find(PortMatcher(vid=0x10C4, pid=0xEA60)) == "COM9"


def test_port_finder_errors() -> None:
    finder = PortFinder(lambda: PORTS)
    with pytest.raises(PortNotFoundError, match="当前系统串口: COM3"):
        finder.find(PortMatcher(description_contains=["FTDI"]), name="dev")
    with pytest.raises(ConfigError):
        finder.find(PortMatcher(), name="dev")
    with pytest.raises(ConfigError):
        PortMatcher(pid=1)

    def broken() -> list:
        raise OSError("boom")

    with pytest.raises(PortNotFoundError, match="枚举系统串口失败"):
        PortFinder(broken).find(PortMatcher(vid=1))


# ================================================================ SerialTransport
def test_open_retries_then_succeeds() -> None:
    factory = FakeSerialFactory(fail_times=2)
    sleeps: list[float] = []
    t = SerialTransport("COM1", 9600, factory=factory, open_retries=3, retry_delay_s=0.5, sleep=sleeps.append)
    t.open()
    assert t.is_open and factory.attempts == 3 and sleeps == [0.5, 0.5]
    assert factory.last.kwargs["baudrate"] == 9600


def test_open_gives_up() -> None:
    t = SerialTransport("COM1", 9600, factory=FakeSerialFactory(fail_times=9), open_retries=2, sleep=lambda _s: None)
    with pytest.raises(SerialOpenError, match="COM1"):
        t.open()
    assert not t.is_open


def test_close_idempotent_and_swallows_errors(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.fail_on["close"] = OSError("handle gone")
    t.close()
    t.close()
    assert not t.is_open and fake_factory.last.close_calls == 1


def test_not_open_raises_disconnected(make_transport) -> None:
    t = make_transport(open=False)
    with pytest.raises(SerialDisconnectedError, match="未打开"):
        t.write(b"x")


@pytest.mark.parametrize(
    ("op", "exc", "expected"),
    [
        ("write", serial.SerialTimeoutException("wt"), SerialTimeoutError),
        ("write", serial.SerialException("gone"), SerialDisconnectedError),
        ("read", OSError("io"), SerialDisconnectedError),
        ("in_waiting", TypeError("handle None"), SerialDisconnectedError),
        ("readline", AttributeError("x"), SerialDisconnectedError),
    ],
)
def test_error_mapping(make_transport, fake_factory, op, exc, expected) -> None:
    t = make_transport()
    fake_factory.last.fail_on[op] = exc
    with pytest.raises(expected):
        if op == "write":
            t.write(b"\x01")
        elif op == "read":
            t.read(1)
        elif op == "in_waiting":
            _ = t.in_waiting
        else:
            t.readline()


def test_short_write_is_timeout(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.short_write = True
    with pytest.raises(SerialTimeoutError, match="仅写入"):
        t.write(b"\x01\x02")


def test_transact_and_read_until(make_transport, fake_factory) -> None:
    t = make_transport()
    fake = fake_factory.last
    fake.feed(b"stale")
    fake.responder = lambda req: b"\xa5" + req + b"\x55"
    resp = t.transact(b"\x01\x02", 0.2, lambda b: b.endswith(b"\x55"))
    assert resp == b"\xa5\x01\x02\x55"
    assert t.read_until(0.0) == b""


def test_read_until_idle_gap() -> None:
    factory = FakeSerialFactory()
    t = SerialTransport("X", 1, factory=factory)
    t.open()
    factory.last.feed(b"abc")
    t0 = time.monotonic()
    assert t.read_until(2.0, idle_gap_s=0.02) == b"abc"
    assert time.monotonic() - t0 < 1.0


def test_reconnect_switches_port(make_transport, fake_factory) -> None:
    t = make_transport("COM1")
    first = fake_factory.last
    t.reconnect("COM2")
    assert not first.is_open and fake_factory.last.port == "COM2" and t.port == "COM2"


def test_open_endpoint_and_endpoint_validation(fake_factory) -> None:
    ep = SerialEndpoint(match=PortMatcher(description_contains=["CH340"]), baudrate=9600)
    t = open_endpoint(ep, name="relay", finder=PortFinder(lambda: PORTS), factory=fake_factory)
    assert t.port == "COM3" and t.is_open and t.name == "relay"
    with pytest.raises(ConfigError):
        SerialEndpoint(baudrate=0)
    with pytest.raises(ConfigError):
        SerialEndpoint(encoding="no-such-codec")


# ================================================================ Relay
def _cmd_cfg(**kw) -> RelayConfig:
    base = dict(channels={1: ChannelCommands(on=0x50, off=0x4F)}, post_write_delay_s=0, drain_response=True)
    base.update(kw)
    return RelayConfig(**base)


def test_command_relay_on_off_repeat(make_transport, fake_factory) -> None:
    t = make_transport()
    relay = CommandRelay(t, _cmd_cfg(repeat=2), sleep=lambda _s: None)
    relay.on(1)
    relay.off(1)
    assert fake_factory.last.tx == [b"\x50", b"\x50", b"\x4f", b"\x4f"]
    assert relay.state == {1: False}
    with pytest.raises(RelayError, match="未配置通道"):
        relay.on(2)


def test_named_commands(make_transport, fake_factory) -> None:
    relay = CommandRelay(make_transport(), _cmd_cfg(commands={"init": "ascii:AT"}), sleep=lambda _s: None)
    relay.send("init")
    assert fake_factory.last.tx == [b"AT"]
    with pytest.raises(RelayError, match="未配置的继电器命名指令"):
        relay.send("nope")


def test_relay_retry_reopens_after_failure(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.fail_on["write"] = serial.SerialException("unplugged")
    relay = CommandRelay(t, _cmd_cfg(send_retries=2), sleep=lambda _s: None)
    relay.on(1)
    assert len(fake_factory.opened) == 2 and fake_factory.last.tx == [b"\x50"]


def test_relay_gives_up(make_transport, fake_factory) -> None:
    fake_factory.setup = lambda h: h.fail_on.update(write=serial.SerialException("dead"))
    relay = CommandRelay(make_transport(), _cmd_cfg(send_retries=2), sleep=lambda _s: None)
    with pytest.raises(RelayError, match="发送失败"):
        relay.on(1)


def test_open_per_command_closes(make_transport, fake_factory) -> None:
    t = make_transport(open=False)
    relay = CommandRelay(t, _cmd_cfg(open_per_command=True), sleep=lambda _s: None)
    relay.on(1)
    assert not t.is_open and fake_factory.last.tx == [b"\x50"]


def test_press_releases_even_if_hold_interrupted(make_transport, fake_factory) -> None:
    def sleeper(_s: float) -> None:
        raise KeyboardInterrupt

    relay = CommandRelay(make_transport(), _cmd_cfg(), sleep=sleeper)
    with pytest.raises(KeyboardInterrupt):
        relay.press(1, 0.3)
    assert fake_factory.last.tx == [b"\x50", b"\x4f"]


def test_all_off_continues_on_failure(make_transport, fake_factory) -> None:
    cfg = _cmd_cfg(channels={1: ChannelCommands(0x01, 0x02), 2: ChannelCommands(0x03, 0x04)})
    relay = CommandRelay(make_transport(), cfg, sleep=lambda _s: None)
    calls: list[int] = []
    orig = relay.off

    def flaky_off(ch: int = 1) -> None:
        calls.append(ch)
        if ch == 1:
            raise RelayError("x")
        orig(ch)

    relay.off = flaky_off  # type: ignore[method-assign]
    relay.all_off()
    assert calls == [1, 2] and fake_factory.last.tx == [b"\x04"]


def test_icse_a0() -> None:
    assert icse_a0_frame(2, True) == bytes([0xA0, 0x02, 0x01, 0xA3])
    assert icse_a0_frame(1, False) == bytes([0xA0, 0x01, 0x00, 0xA1])
    assert icse_a0_frame(0x7F, True)[-1] == (0xA0 + 0x7F + 1) & 0xFF
    with pytest.raises(ConfigError):
        icse_a0_frame(0, True)


def test_build_relay_types(make_transport) -> None:
    t = make_transport()
    assert isinstance(build_relay(RelayConfig(type="icse_a0"), t), IcseA0Relay)
    assert isinstance(build_relay(_cmd_cfg(), t), CommandRelay)
    with pytest.raises(ConfigError):
        RelayConfig(type="command")
    with pytest.raises(ConfigError):
        RelayConfig(channels={1: ChannelCommands("ZZ", 1)})


def test_servo(make_transport, fake_factory) -> None:
    assert servo_command(0, 2500, 1000) == b"#000P2500T1000!"
    for bad in ((300, 0, 1), (0, 100, 1), (0, 1500, 10000)):
        with pytest.raises(ConfigError):
            servo_command(*bad)
    t = make_transport(open=False)
    servo = ServoDriver(t, sleep=lambda _s: None)
    servo.move(1500, 500)
    assert fake_factory.last.tx == [b"#000P1500T0500!"] and not t.is_open


def test_servo_retry_exhausted(make_transport, fake_factory) -> None:
    fake_factory.setup = lambda h: h.fail_on.update(write=OSError("x"))
    servo = ServoDriver(make_transport(open=False), retries=2, sleep=lambda _s: None)
    with pytest.raises(RelayError):
        servo.move(1500, 500)
    assert len(fake_factory.opened) == 2


# ================================================================ Monitor
def _monitor(t: SerialTransport, **cfg) -> DeviceLogMonitor:
    return DeviceLogMonitor(t, KeywordEvaluator(KeywordConfig(**cfg)), poll_s=0.001)


def test_watch_success_stop(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.feed("boot\r\n\x1b[32mvoice_msgnum:9\x1b[0m\r\nlater\r\n")
    res = _monitor(t, success=["voice_msgnum:9"]).watch(2.0, stop_on_success=True)
    assert res.success == "voice_msgnum:9" and res.any_data and res.lines == ["boot", "voice_msgnum:9"]
    assert res.elapsed_s < 1.0


def test_watch_abort(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.feed("ok\nHARDFAULT\nmore\n")
    res = _monitor(t, abort=["hardfault"]).watch(2.0)
    assert res.aborted and res.abort_keep_power and res.lines[-1] == "HARDFAULT"


def test_watch_disconnect(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.fail_on["in_waiting"] = serial.SerialException("unplugged")
    res = _monitor(t).watch(2.0)
    assert res.disconnected and "unplugged" in (res.disconnect_error or "")


def test_watch_no_data_warning(make_transport, caplog: pytest.LogCaptureFixture) -> None:
    t = make_transport()
    mon = DeviceLogMonitor(t, KeywordEvaluator(KeywordConfig()), poll_s=0.001, no_data_warning_s=0.01)
    with caplog.at_level("ERROR"):
        res = mon.watch(0.05)
    assert not res.any_data and "串口静默预警" in caplog.text


def test_watch_split_success_via_buffer(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.feed("voice_ms\ng num: 1\n")
    res = _monitor(t, success=["voice_msg num: 1"]).watch(0.2, stop_on_success=True)
    assert res.success == "voice_msg num: 1"


def test_background_reader_abort_and_snapshot(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.feed("line1\nFATAL here\n")
    reader = BackgroundLogReader(_monitor(t, abort=["fatal"]))
    reader.start()
    assert reader.abort_event.wait(2.0)
    reader.stop()
    assert "FATAL here" in reader.snapshot(clear=True) and reader.snapshot() == []


def test_background_reader_reconnects(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.fail_on["in_waiting"] = serial.SerialException("gone")
    recovered = threading.Event()

    def reconnect() -> bool:
        t.reconnect()
        fake_factory.last.feed("back\n")
        recovered.set()
        return True

    reader = BackgroundLogReader(_monitor(t), reconnect=reconnect, reconnect_interval_s=0.01)
    reader.start()
    assert recovered.wait(2.0)
    deadline = time.monotonic() + 2
    while "back" not in reader.snapshot() and time.monotonic() < deadline:
        time.sleep(0.01)
    reader.stop()
    assert "back" in reader.snapshot() and not reader.failed_event.is_set()


def test_background_reader_fails_without_reconnect(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.fail_on["in_waiting"] = OSError("gone")
    reader = BackgroundLogReader(_monitor(t))
    reader.start()
    assert reader.failed_event.wait(2.0)
    reader.stop()


def test_background_reader_unexpected_error_sets_failed(make_transport, fake_factory) -> None:
    t = make_transport()
    fake_factory.last.feed("x\n")

    def bad_callback(_line, _verdict):
        raise RuntimeError("callback bug")

    mon = _monitor(t)
    mon.on_line = bad_callback
    reader = BackgroundLogReader(mon)
    reader.start()
    assert reader.failed_event.wait(2.0)
    reader.stop()
