"""大灯 / 灯光继电器闪烁压力测试工位。

合并旧脚本（方案均在 config/stations/headlight.yaml 的 ``lamp.variants`` 中配置）：

* Tool/TestHeadlights/test_HeadlightsSpecificationIntervals.py   -> ``spec``
* Tool/TestHeadlights/test_HeadlightsNoSpecificationIntervals.py -> ``no_spec``

运行::

    ppx-test run headlight --set lamp.variant=no_spec
"""

from __future__ import annotations

from ppx_testkit.apps.lamp._common import run_lamp
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

TITLE = "大灯闪烁压力测试"


def run(settings: AppSettings, ctx: RunContext) -> int:
    return run_lamp(settings, ctx, title=TITLE)
