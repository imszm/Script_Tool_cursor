"""Windows 网页停止链路：Ctrl+Break 能打断主线程等待，工位收尾断开继电器后退出。

用 ``loop://`` 代替真实继电器串口，不接硬件。需要本机控制台转发 Ctrl+Break，CI 上跳过。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from ppx_testkit.web.session import RunSession

pytestmark = pytest.mark.skipif(
    os.name != "nt" or bool(os.environ.get("CI")),
    reason="需要本机 Windows 控制台；CI 上控制台信号不可靠",
)


def _wait_for(session: RunSession, text: str, timeout_s: float) -> bool:
    """轮询控制台输出，直到出现 ``text``。

    :param session: 运行会话。
    :param text: 要等的片段。
    :param timeout_s: 最长等待秒数。
    :return: 等到返回 True，超时返回 False。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if any(text in line for line in session.log_from(0)["lines"]):
            return True
        time.sleep(0.05)
    return False


def test_web_stop_reaches_relay_teardown(tmp_path: Path) -> None:
    args = [
        sys.executable, "-m", "ppx_testkit", "run", "relay_power_cycle", "--no-local",
        "--port", "relay=loop://",
        "--set", "test.monitor_device=false",
        "--set", "test.precheck=false",
        "--set", "test.fail_keeps_power=false",
        "--set", "test.stop_on_fail=false",
        "--set", "test.power_on_s=10",
        "--set", "test.cycles=1",
        "--set", "test.notify=false",
        "--set", f"logging.root_dir={tmp_path.as_posix()}",
    ]
    # pytest 只给当前进程加 src，子进程要自己能 import ppx_testkit。
    src = Path(__file__).resolve().parents[2] / "src"
    previous = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(src) if not previous else str(src) + os.pathsep + previous
    session = RunSession()
    try:
        session.start("relay_power_cycle", args, tmp_path / "relay_power_cycle")
        assert _wait_for(session, "CH1 -> ON", 30), session.log_from(0)["lines"]
        started = time.monotonic()
        session.stop(grace_s=10)
        elapsed = time.monotonic() - started
    finally:
        session.stop(grace_s=2)
        if previous is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous
    assert _wait_for(session, "CH1 -> OFF", 5), session.log_from(0)["lines"]
    assert not session.forced
    assert session.exit_code == 130
    assert elapsed < 3, elapsed
