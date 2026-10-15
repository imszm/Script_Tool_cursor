"""R3 MCB 交互菜单。由 R3McbTester 继承，选项顺序与旧脚本一致。"""

from __future__ import annotations

from ppx_testkit.apps.whitebox.r3_names import MENU, log
from ppx_testkit.exceptions import (
    TestAbort,
    ToolError,
)


class R3MenuCases:
    # ------------------------------------------------------------ 交互
    def interactive(self) -> None:
        while True:
            log.info(MENU)
            try:
                ch = self._input("请选择操作: ").strip().lower()
                if ch == "q":
                    log.info("退出程序。")
                    return
                self._dispatch(ch)
            except EOFError:
                log.info("输入结束，退出交互模式。")
                return
            except ValueError as exc:
                log.error("输入错误: %s", exc)
            except TestAbort:
                raise
            except ToolError as exc:
                log.error("交互操作失败: %s", exc)

    def _dispatch(self, ch: str) -> None:
        cfg = self.cfg
        if ch == "0":
            self.read_motor_status()
        elif ch == "1":
            self.start_motor_speed_mode(cfg.low_speed_rpm)
        elif ch == "2":
            self.start_motor_speed_mode(cfg.mid_speed_rpm)
        elif ch == "3":
            self.start_motor_speed_mode(int(self._input("目标速度 (rpm): ")))
        elif ch == "4":
            self.stop_motor()
        elif ch == "5":
            r = cfg.ramp
            mx = self._input(f"最大速度 (默认{r.max_speed}): ").strip()
            st = self._input(f"步进 (默认{r.step}): ").strip()
            dw = self._input(f"停留秒数 (默认{r.dwell_s}): ").strip()
            self.ramp_test(int(mx) if mx else r.max_speed, int(st) if st else r.step, float(dw) if dw else r.dwell_s)
        elif ch == "6":
            cur_a = float(self._input("目标电流 (A): "))
            self.set_target_current(int(cur_a * 10))
        elif ch == "7":
            self.clear_error_code()
        elif ch == "8":
            log.info("运行模式: 0-空闲 2-骑行 4-推行 10-延迟关刹 11-立即关刹 12-解锁")
            self.set_run_mode(int(self._input("模式编号: ")))
        elif ch == "9":
            self.read_brake_state()
        elif ch == "a":
            self.run_full()
        else:
            log.warning("未知选项。")
