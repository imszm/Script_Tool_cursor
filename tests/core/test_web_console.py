"""网页运行台：不接硬件，检查工位表单、命令参数、单次运行锁和停止宽限。"""

from __future__ import annotations

import http.client
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

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
from ppx_testkit.web.server import ConsoleServer, _page_path
from ppx_testkit.web.session import RunSession


def test_catalog_includes_charge_stations() -> None:
    rows = station_catalog()
    names = {row["name"] for row in rows}
    assert {"l5_charge_cycle", "r3_charge_cycle", "relay_power_cycle"} <= names
    relay = next(row for row in rows if row["name"] == "relay_power_cycle")
    assert {item["name"] for item in relay["serial"]} == {"relay", "device"}
    assert relay["cycle_key"] == "test.cycles"
    assert relay["caution"] is None
    probe = next(row for row in rows if row["name"] == "relay_probe")
    pctool = next(row for row in rows if row["name"] == "ppx_pctool_stress")
    assert probe["caution"]
    assert pctool["caution"]


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
    return Path("logs") / "web_console_test"


class _FakeProc:
    """假子进程：收到中断后按需在 exit_delay_s 秒后以 130 退出，否则一直不退。"""

    def __init__(self, *, exits_on_interrupt: bool, exit_delay_s: float = 0.0) -> None:
        self.pid = 4242
        self.stdout = None
        self.returncode: int | None = None
        self.interrupts = 0
        self.terminated = False
        self._exits_on_interrupt = exits_on_interrupt
        self._exit_delay_s = exit_delay_s
        self._done = threading.Event()

    def interrupt(self) -> None:
        """记一次停止信号；会退出的进程稍后以 130 结束。"""
        self.interrupts += 1
        if self._exits_on_interrupt:
            threading.Timer(self._exit_delay_s, self._finish, args=(130,)).start()

    def _finish(self, code: int) -> None:
        """记下退出码并唤醒 wait。

        :param code: 退出码。
        """
        if self.returncode is None:
            self.returncode = code
        self._done.set()

    def poll(self) -> int | None:
        """返回退出码；还在运行时为 None。"""
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        """等待退出，超时抛 TimeoutExpired。

        :param timeout: 最长等待秒数。
        :return: 退出码。
        """
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return self.returncode

    def terminate(self) -> None:
        """强制结束，退出码与 Windows TerminateProcess 一致为 1。"""
        self.terminated = True
        self._finish(1)

    def kill(self) -> None:
        """强杀。"""
        self._finish(1)


def _fake_session(proc: _FakeProc) -> RunSession:
    """返回只会启动 ``proc`` 的会话，中断直接调到假进程上。

    :param proc: 假子进程。
    :return: 会话。
    """
    def _popen(*_args: Any, **_kwargs: Any) -> Any:
        return proc

    return RunSession(popen=_popen, interrupt=lambda p: p.interrupt())  # type: ignore[attr-defined]


def test_stop_forces_when_station_ignores_interrupt(tmp_path: Path) -> None:
    proc = _FakeProc(exits_on_interrupt=False)
    session = _fake_session(proc)
    session.start("demo", ["python"], tmp_path)
    session.stop(grace_s=0.05)
    snap = session.snapshot()
    assert proc.interrupts == 1 and proc.terminated
    assert snap["forced"] is True and snap["stop_requested"] is True
    assert snap["running"] is False and snap["exit_code"] == 1


def test_concurrent_stop_sends_one_interrupt(tmp_path: Path) -> None:
    proc = _FakeProc(exits_on_interrupt=True, exit_delay_s=0.2)
    session = _fake_session(proc)
    session.start("demo", ["python"], tmp_path)
    threads = [threading.Thread(target=session.stop, kwargs={"grace_s": 3}) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    snap = session.snapshot()
    assert proc.interrupts == 1 and not proc.terminated
    assert snap["forced"] is False and snap["exit_code"] == 130


def _http_status(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> int:
    """向本机网页发一次请求，返回 HTTP 状态码。

    :param port: 网页端口。
    :param method: ``GET`` 或 ``POST``。
    :param path: 请求路径。
    :param headers: 额外请求头。
    :param body: 请求体。
    :return: 状态码。
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def test_console_rejects_foreign_origin_host_and_content_type() -> None:
    """外部网页、伪造 Host 和 text/plain 不能触发运行台；本页停止请求放行。"""
    server = ConsoleServer(("127.0.0.1", 0), RunSession())
    thread = threading.Thread(target=server.serve_forever, name="ppx-web-test", daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        foreign = _http_status(
            port, "POST", "/api/stop",
            headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
            body=b"{}",
        )
        local = _http_status(
            port, "POST", "/api/stop",
            headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"},
            body=b"{}",
        )
        forged_host = _http_status(port, "GET", "/api/status", headers={"Host": "evil.example"})
        plain = _http_status(
            port, "POST", "/api/start",
            headers={"Content-Type": "text/plain"},
            body=b"{}",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert foreign == 403
    assert local == 200
    assert forged_host == 403
    assert plain == 415


def test_new_run_clears_stop_flags(tmp_path: Path) -> None:
    first = _FakeProc(exits_on_interrupt=False)
    session = _fake_session(first)
    session.start("demo", ["python"], tmp_path)
    session.stop(grace_s=0.01)
    second = _FakeProc(exits_on_interrupt=True)
    session._popen = lambda *_a, **_k: second  # type: ignore[method-assign]
    session.start("demo", ["python"], tmp_path)
    snap = session.snapshot()
    assert snap["stop_requested"] is False and snap["forced"] is False and snap["running"] is True
    session.stop(grace_s=1)
