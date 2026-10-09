"""辅助工具：定时打印鼠标当前坐标，用于标定工位配置中的点击 / 取色坐标。

迁移自 ``Tool/鼠标定位脚本.py``。按 Ctrl+C 结束（正常退出，返回 0）；
``locate.max_samples > 0`` 时采样指定次数后自动结束。采样结果另存为 CSV。
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ppx_testkit.core.report.writers import write_csv
from ppx_testkit.exceptions import ConfigError, GuiError
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)


class PositionSource(Protocol):
    def position(self) -> tuple[int, int]: ...


@dataclass(frozen=True)
class LocateConfig:
    interval_s: float = 2.0
    max_samples: int = 0
    output_name: str = "mouse_positions.csv"

    def __post_init__(self) -> None:
        if self.interval_s <= 0:
            raise ConfigError("locate.interval_s 必须 > 0")
        if self.max_samples < 0:
            raise ConfigError("locate.max_samples 不能为负数（0 表示直到 Ctrl+C）")


class ScreenPosition:
    """core Screen 未提供鼠标位置接口，这里直接读取其 pyautogui 句柄。"""

    def __init__(self, screen: Any) -> None:
        self._pg = screen.pg

    def position(self) -> tuple[int, int]:
        try:
            x, y = self._pg.position()
        except Exception as exc:  # noqa: BLE001
            raise GuiError(f"读取鼠标位置失败: {exc}") from exc
        return int(x), int(y)


def locate_loop(source: PositionSource, cfg: LocateConfig, *, sleep: Callable[[float], None] = time.sleep,
                now: Callable[[], _dt.datetime] = _dt.datetime.now) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    try:
        while cfg.max_samples == 0 or len(samples) < cfg.max_samples:
            x, y = source.position()
            samples.append({"time": now().strftime("%Y-%m-%d %H:%M:%S"), "x": x, "y": y})
            log.info("Mouse position: X=%d, Y=%d", x, y)
            if cfg.max_samples and len(samples) >= cfg.max_samples:
                break
            sleep(cfg.interval_s)
    except KeyboardInterrupt:
        log.info("Program terminated.（用户 Ctrl+C 结束，共采样 %d 次）", len(samples))
    return samples


def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("locate", LocateConfig, required=False)
    from ppx_testkit.core.gui.screen import Screen

    samples = locate_loop(ScreenPosition(Screen()), cfg)
    path = ctx.artifact(cfg.output_name)
    if path is not None and samples:
        write_csv(path, samples, ["time", "x", "y"])
    return 0
