from __future__ import annotations

"""
兼容入口（原文件名不变）：
- 旧脚本为“导入即执行”，不利于工程化复用
- 现在改为薄封装，转到统一 CLI：`python -m script_tool pc-upgrade ...`
"""

from script_tool.cli import main as cli_main


APP_TITLE = "L5 PCTOOL V3.9.00"
CYCLE_WAIT = 170


if __name__ == "__main__":
    raise SystemExit(
        cli_main(
            [
                "pc-upgrade",
                "--loops",
                "10",
            ]
        )
    )

