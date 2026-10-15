"""辅助工具：列出目标窗口的全部控件（类型 / 文本 / auto_id），用于编写 pywinauto 工位配置。

迁移自 ``L5系列SMT&组装升级测试工具/识别读取应用窗口控件.py``。
按 ``window.title`` 连接进程，可用 ``window.window_title`` 指定实际要遍历的主窗口；
结果写入日志并保存为运行目录下的 ``inspect.output_name``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ppx_testkit.apps.pctool.common import PcToolWindow, ToolWindow, WindowConfig
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.logger import RunContext
from ppx_testkit.settings import AppSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InspectConfig:
    output_name: str = "controls.txt"

    def __post_init__(self) -> None:
        if not self.output_name or "/" in self.output_name or "\\" in self.output_name:
            raise ConfigError(f"inspect.output_name 只能是文件名: {self.output_name!r}")


def inspect_window(window: ToolWindow, cfg: InspectConfig, ctx: RunContext | None) -> str:
    window.connect()
    text = window.dump_controls()
    lines = text.splitlines()
    log.info("共识别到 %d 个控件：\n%s", len(lines), text)
    if ctx is not None:
        path = ctx.write_text(cfg.output_name, text + "\n")
        if path is not None:
            log.info("控件清单已保存: %s", path)
    return text


def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("inspect", InspectConfig, required=False)
    window = PcToolWindow(settings.section("window", WindowConfig))
    inspect_window(window, cfg, ctx)
    return 0
