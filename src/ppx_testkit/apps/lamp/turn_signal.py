"""转向灯压力测试工位。

合并旧脚本（方案均在 config/stations/turn_signal.yaml 的 ``lamp.variants`` 中配置）：

* Tool/左右转向灯自动化测试-正式版本.py                      -> ``formal``
* Tool/TestTurnSignals/test_LeftTurnSignalsSpecificationIntervals.py    -> ``left_spec``
* Tool/TestTurnSignals/test_RightTurnSignalsSpecificationIntervals.py   -> ``right_spec``
* Tool/TestTurnSignals/test_LeftTurnSignalsNoSpecificationIntervals.py  -> ``left_no_spec``
* Tool/TestTurnSignals/test_RightTurnSignalsNoSpecificationIntervals.py -> ``right_no_spec``

运行::

    ppx-test run turn_signal --set lamp.variant=left_spec --set lamp.cycles=99
"""

from __future__ import annotations

from ppx_testkit.apps.lamp._common import run_lamp
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

TITLE = "转向灯压力测试"


def run(settings: AppSettings, ctx: RunContext) -> int:
    return run_lamp(settings, ctx, title=TITLE)
