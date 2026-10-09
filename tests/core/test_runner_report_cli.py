from __future__ import annotations

import json
import sys
import textwrap
import types
from pathlib import Path

import pytest

from ppx_testkit import cli
from ppx_testkit.core.report.case_loader import load_cases, to_bool_int, to_float, to_int
from ppx_testkit.core.report.writers import write_csv, write_html
from ppx_testkit.core.runner.result import CycleResult, RunSummary
from ppx_testkit.core.runner.stress_runner import StressRunner
from ppx_testkit.exceptions import ConfigError, SerialDisconnectedError, TestAbort
from ppx_testkit.logger import setup_logging, shutdown_logging
from ppx_testkit.settings import LoggingSettings


# ================================================================ StressRunner
class ScriptedCycle:
    def __init__(self, outcomes: list, setup_exc: BaseException | None = None) -> None:
        self.outcomes = outcomes
        self.setup_exc = setup_exc
        self.calls: list[str] = []
        self.teardown_summary: RunSummary | None = None

    def setup(self) -> None:
        self.calls.append("setup")
        if self.setup_exc:
            raise self.setup_exc

    def run_cycle(self, index: int) -> CycleResult:
        self.calls.append(f"cycle{index}")
        out = self.outcomes[index - 1]
        if isinstance(out, BaseException):
            raise out
        return CycleResult(index, out, "" if out else "未检测到成功关键字")

    def teardown(self, summary: RunSummary) -> None:
        self.calls.append("teardown")
        self.teardown_summary = summary


def test_runner_counts_and_summary_json(tmp_path: Path) -> None:
    ctx = setup_logging("st", LoggingSettings(root_dir=str(tmp_path)), console=False)
    cyc = ScriptedCycle([True, False, True])
    s = StressRunner(cyc, 3, station="st").run()
    shutdown_logging()
    assert (s.executed, s.passed, s.failed) == (3, 2, 1)
    assert round(s.pass_rate, 2) == 66.67 and not s.ok
    assert cyc.calls == ["setup", "cycle1", "cycle2", "cycle3", "teardown"]
    data = json.loads((ctx.run_dir / "summary.json").read_text(encoding="utf-8"))  # type: ignore[operator]
    assert data["passed"] == 2 and data["ok"] is False


def test_runner_all_pass_ok() -> None:
    s = StressRunner(ScriptedCycle([True, True]), 2, station="st").run()
    assert s.ok and s.pass_rate == 100.0


def test_runner_stop_on_fail_keeps_power() -> None:
    cyc = ScriptedCycle([True, False, True])
    s = StressRunner(cyc, 3, station="st", stop_on_fail=True, fail_keeps_power=True).run()
    assert s.aborted and s.keep_power and s.executed == 2 and "第 2 轮失败" in (s.abort_reason or "")
    assert cyc.calls[-1] == "teardown" and cyc.teardown_summary is s


def test_runner_max_consecutive_failures() -> None:
    s = StressRunner(ScriptedCycle([False, True, False, False, True]), 5, station="st",
                     max_consecutive_failures=2).run()
    assert s.aborted and s.executed == 4 and not s.keep_power


def test_runner_test_abort_from_cycle() -> None:
    s = StressRunner(ScriptedCycle([True, TestAbort("致命关键字", keep_power=True)]), 3, station="st").run()
    assert s.aborted and s.keep_power and s.abort_reason == "致命关键字" and s.executed == 1


def test_runner_keyboard_interrupt() -> None:
    cyc = ScriptedCycle([KeyboardInterrupt()])
    s = StressRunner(cyc, 2, station="st").run()
    assert s.interrupted and not s.keep_power and cyc.calls[-1] == "teardown"


def test_runner_hardware_error_and_setup_failure() -> None:
    s = StressRunner(ScriptedCycle([SerialDisconnectedError("COM3", "gone")]), 2, station="st").run()
    assert s.error and "SerialDisconnectedError" in s.error
    cyc = ScriptedCycle([], setup_exc=SerialDisconnectedError("COM3", "open fail"))
    s2 = StressRunner(cyc, 2, station="st").run()
    assert s2.executed == 0 and s2.error and cyc.calls == ["setup", "teardown"]


def test_runner_unexpected_and_failsafe() -> None:
    s = StressRunner(ScriptedCycle([ValueError("bug")]), 1, station="st").run()
    assert s.error and "ValueError" in s.error
    failsafe = type("FailSafeException", (Exception,), {})
    s2 = StressRunner(ScriptedCycle([failsafe()]), 1, station="st").run()
    assert s2.interrupted and not s2.error


def test_runner_teardown_exception_does_not_mask_result() -> None:
    cyc = ScriptedCycle([True])

    def bad_teardown(_s: RunSummary) -> None:
        raise RuntimeError("teardown bug")

    cyc.teardown = bad_teardown  # type: ignore[method-assign]
    s = StressRunner(cyc, 1, station="st").run()
    assert s.passed == 1 and s.ok


def test_runner_stop_event() -> None:
    runner = StressRunner(ScriptedCycle([True] * 5), 5, station="st", interval_s=5)
    runner.stop_event.set()
    s = runner.run()
    assert s.interrupted and s.executed == 0
    with pytest.raises(ValueError):
        StressRunner(ScriptedCycle([]), 0, station="st")


def test_summary_render() -> None:
    s = RunSummary(station="st", target_cycles=10, executed=2, passed=1, failed=1, aborted=True,
                   abort_reason="x", keep_power=True, extra={"异常次数": 3})
    text = s.render()
    assert "通过率 50.00%" in text and "熔断原因" in text and "异常次数: 3" in text


# ================================================================ report
def test_write_csv_and_html(tmp_path: Path) -> None:
    rows = [{"case": "A", "verdict": "PASS", "detail": {"k": 1}}, {"case": "<B>", "verdict": "FAIL", "detail": None}]
    p = write_csv(tmp_path / "r" / "out.csv", rows)
    assert p and p.read_bytes().startswith(b"\xef\xbb\xbf")
    text = p.read_text(encoding="utf-8-sig")
    assert 'case,verdict,detail' in text and '"{""k"": 1}"' in text
    h = write_html(tmp_path / "out.html", title="报告", summary={"通过": 1}, rows=rows)
    assert h is not None
    content = h.read_text(encoding="utf-8")
    assert 'class="pass"' in content and 'class="fail"' in content and "&lt;B&gt;" in content and "报告" in content
    assert "$" not in content.split("<script")[0]


def test_writers_swallow_os_errors(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert write_csv(blocker / "sub" / "a.csv", [{"a": 1}]) is None
    assert write_html(blocker / "sub" / "a.html", title="t", summary={}, rows=[]) is None


def test_load_cases_csv_encodings(tmp_path: Path) -> None:
    content = "用例,期望\n亮度,1\n,\n灯环,0\n"
    for enc in ("utf-8-sig", "gb18030"):
        p = tmp_path / f"{enc}.csv"
        p.write_bytes(content.encode(enc))
        assert load_cases(p) == [{"用例": "亮度", "期望": "1"}, {"用例": "灯环", "期望": "0"}]


def test_load_cases_xlsx(tmp_path: Path) -> None:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    p = tmp_path / "c.xlsx"
    pd.DataFrame({" 用例 ": ["a", None], "值": [1, None]}).to_excel(p, index=False)
    assert load_cases(p) == [{"用例": "a", "值": 1}]


def test_load_cases_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        load_cases(tmp_path / "x.csv")
    (tmp_path / "x.txt").write_text("a", encoding="utf-8")
    with pytest.raises(ConfigError, match="不支持"):
        load_cases(tmp_path / "x.txt")
    (tmp_path / "e.csv").write_text("a,b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="为空"):
        load_cases(tmp_path / "e.csv")


def test_value_converters() -> None:
    assert [to_int(v) for v in ("0x10", " 3 ", "2.7", "", None, float("nan"), "abc", True, 4.9)] == [
        16, 3, 2, None, None, None, None, 1, 4]
    assert [to_float(v) for v in ("1.5", "", None, "x")] == [1.5, None, None, None]
    assert [to_bool_int(v) for v in ("是", "OFF", 2, 0.0, "", "2", "maybe")] == [1, 0, 1, 0, None, 1, None]


def test_bundled_testcases_load() -> None:
    from ppx_testkit.utils.paths import resources_dir

    assert load_cases(resources_dir() / "testcases" / "testcases-模板.csv")


# ================================================================ CLI
def _station(project: Path, name: str, body: str) -> None:
    (project / "config" / "stations" / f"{name}.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def fake_app(monkeypatch: pytest.MonkeyPatch):
    calls: dict = {}
    mod = types.ModuleType("ppx_testkit.apps._fake_app")

    def run(settings, ctx):
        calls["settings"], calls["ctx"] = settings, ctx
        behaviour = settings.raw("behaviour", "ok")
        if behaviour == "fail":
            return 1
        if behaviour == "hw":
            raise SerialDisconnectedError("COM1", "gone")
        if behaviour == "config":
            raise ConfigError("bad")
        if behaviour == "ctrlc":
            raise KeyboardInterrupt
        if behaviour == "bug":
            raise RuntimeError("bug")
        return 0

    mod.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ppx_testkit.apps._fake_app", mod)
    return calls


@pytest.mark.parametrize(
    ("behaviour", "code"),
    [("ok", 0), ("fail", 1), ("hw", 3), ("config", 2), ("ctrlc", 130), ("bug", 1)],
)
def test_cli_run_exit_codes(project_tmp: Path, fake_app, behaviour: str, code: int) -> None:
    _station(project_tmp, "demo", f"app: _fake_app\nbehaviour: {behaviour}\nlogging: {{root_dir: {project_tmp}/logs}}\n")
    assert cli.main(["run", "demo", "--no-local"]) == code


def test_cli_run_writes_config_and_applies_overrides(project_tmp: Path, fake_app) -> None:
    _station(project_tmp, "demo", f"app: _fake_app\nlogging: {{root_dir: {project_tmp}/logs}}\n")
    assert cli.main(["run", "demo", "--set", "test.cycles=5", "--port", "relay=COM9"]) == 0
    s, ctx = fake_app["settings"], fake_app["ctx"]
    assert s.raw("test.cycles") == 5 and s.raw("serial.relay.match.port") == "COM9"
    cfg = (ctx.run_dir / "config.yaml").read_text(encoding="utf-8")
    assert "COM9" in cfg and (ctx.run_dir / "full.log").exists()


def test_cli_config_errors(project_tmp: Path, capsys: pytest.CaptureFixture) -> None:
    _station(project_tmp, "noapp", "x: 1\n")
    _station(project_tmp, "badmod", "app: does_not_exist\n")
    (project_tmp / "config" / "default.yaml").write_text(f"logging: {{root_dir: {project_tmp}/logs}}\n", encoding="utf-8")
    assert cli.main(["run", "noapp"]) == 2
    assert cli.main(["run", "badmod"]) == 2
    assert cli.main(["run", "missing"]) == 2
    assert cli.main(["show", "missing"]) == 2
    assert cli.main(["run", "badmod", "--port", "nonsense"]) == 2


def test_cli_list_and_show(project_tmp: Path, capsys: pytest.CaptureFixture) -> None:
    _station(project_tmp, "a", "app: x\ndescription: 演示工位\n")
    _station(project_tmp, "broken", "app: [\n")
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "演示工位" in out and "配置错误" in out
    assert cli.main(["show", "a", "--set", "k=1"]) == 0
    out = capsys.readouterr().out
    assert "应用: x" in out and "k: 1" in out


def test_cli_ports(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    port = types.SimpleNamespace(device="COM3", description="CH340", hwid="USB VID:PID=1A86:7523")
    monkeypatch.setattr("ppx_testkit.core.serial.port_finder._default_lister", lambda: [port])
    assert cli.main(["ports"]) == 0
    assert "COM3" in capsys.readouterr().out
