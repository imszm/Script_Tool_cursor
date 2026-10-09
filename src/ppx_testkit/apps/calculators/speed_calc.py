"""代步车固件车速工程计算器（迁移自 Tool/代步车车速计算器.py）。

公式::

    轮毂转速 = 电机转速 / 减速比
    轮胎周长(m) = π × 外径(英寸) × 0.0254
    车速(km/h) = 轮毂转速 × 60 × 周长 / 1000
    车速(mph) = km/h / 1.609344

运行::

    ppx-test run speed_calc                                    # GUI（需要 tkinter）
    ppx-test run speed_calc --set speed_calc.mode=cli --set speed_calc.rpm=4000
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ppx_testkit.core.report.writers import write_csv
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.logger import RunContext, get_logger
from ppx_testkit.settings import AppSettings

log = get_logger(__name__)

INCH_TO_M = 0.0254
KM_PER_MILE = 1.609344


# ============================================================ 纯计算
@dataclass(frozen=True)
class SpeedResult:
    motor_rpm: float
    gear_ratio: float
    wheel_diameter_inch: float
    wheel_rpm: float
    circumference_m: float
    speed_kmh: float
    speed_mph: float


def validate_inputs(gear_ratio: float, wheel_diameter_inch: float) -> None:
    """减速比与外径必须 > 0（电机转速允许为负，表示反转）。"""
    if not (math.isfinite(gear_ratio) and math.isfinite(wheel_diameter_inch)):
        raise ValueError("参数有误: 减速比和外径必须是有限数值")
    if gear_ratio <= 0 or wheel_diameter_inch <= 0:
        raise ValueError("参数有误: 减速比和外径需大于 0")


def compute_speed(motor_rpm: float, gear_ratio: float, wheel_diameter_inch: float) -> SpeedResult:
    validate_inputs(gear_ratio, wheel_diameter_inch)
    if not math.isfinite(motor_rpm):
        raise ValueError("参数有误: 电机转速必须是有限数值")
    wheel_rpm = motor_rpm / gear_ratio
    circumference_m = math.pi * wheel_diameter_inch * INCH_TO_M
    speed_kmh = (wheel_rpm * 60 * circumference_m) / 1000.0
    speed_mph = speed_kmh / KM_PER_MILE
    return SpeedResult(
        motor_rpm=motor_rpm,
        gear_ratio=gear_ratio,
        wheel_diameter_inch=wheel_diameter_inch,
        wheel_rpm=wheel_rpm,
        circumference_m=circumference_m,
        speed_kmh=speed_kmh,
        speed_mph=speed_mph,
    )


def format_result(result: SpeedResult, decimals: int = 2) -> str:
    return f"计算结果: \n{result.speed_kmh:.{decimals}f} km/h\n{result.speed_mph:.{decimals}f} mp/h"


def evaluate_text_inputs(
    rpm_text: str, ratio_text: str, diameter_text: str, decimals: int = 2
) -> tuple[str, SpeedResult | None]:
    """GUI 实时计算：输入框文本 -> (显示文字, 结果)。行为与旧 GUI 一致，输入不完整/非法时不抛异常。"""
    rpm_s, ratio_s, dia_s = rpm_text.strip(), ratio_text.strip(), diameter_text.strip()
    if not rpm_s or not ratio_s or not dia_s:
        return "等待输入完整参数...", None
    try:
        rpm, ratio, dia = float(rpm_s), float(ratio_s), float(dia_s)
    except ValueError:
        return "输入格式不合法...", None
    try:
        result = compute_speed(rpm, ratio, dia)
    except ValueError as exc:
        return str(exc), None
    return format_result(result, decimals), result


# ============================================================ 配置
@dataclass(frozen=True)
class SpeedCase:
    rpm: float
    gear_ratio: float
    wheel_diameter_inch: float
    name: str = ""

    def __post_init__(self) -> None:
        try:
            validate_inputs(self.gear_ratio, self.wheel_diameter_inch)
        except ValueError as exc:
            raise ConfigError(f"车速计算用例 '{self.name or self.rpm}': {exc}") from exc


@dataclass(frozen=True)
class SpeedCalcConfig:
    mode: Literal["gui", "cli"] = "gui"
    rpm: float = 3728.0
    gear_ratio: float = 6.2
    wheel_diameter_inch: float = 8.0
    decimals: int = 2
    cases: list[SpeedCase] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 0 <= self.decimals <= 10:
            raise ConfigError(f"speed_calc.decimals 应在 0~10 之间，实际 {self.decimals}")
        try:
            validate_inputs(self.gear_ratio, self.wheel_diameter_inch)
        except ValueError as exc:
            raise ConfigError(f"speed_calc 默认参数: {exc}") from exc

    def all_cases(self) -> list[SpeedCase]:
        default = SpeedCase(rpm=self.rpm, gear_ratio=self.gear_ratio,
                            wheel_diameter_inch=self.wheel_diameter_inch, name="默认参数")
        return [default, *self.cases]


# ============================================================ 入口
def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("speed_calc", SpeedCalcConfig, required=False)
    if cfg.mode == "gui":
        return _run_gui(cfg)
    return run_cli(cfg, ctx)


def run_cli(cfg: SpeedCalcConfig, ctx: RunContext) -> int:
    rows: list[dict[str, Any]] = []
    for case in cfg.all_cases():
        r = compute_speed(case.rpm, case.gear_ratio, case.wheel_diameter_inch)
        log.info(
            "[%s] 输入: RPM=%s, Ratio=%s, Dia=%s -> 输出: %.*f km/h, %.*f mp/h",
            case.name or "-", r.motor_rpm, r.gear_ratio, r.wheel_diameter_inch,
            cfg.decimals, r.speed_kmh, cfg.decimals, r.speed_mph,
        )
        rows.append({"name": case.name, **{k: round(v, 6) for k, v in asdict(r).items()}})
    ctx.write_json("speed_calc.json", rows)
    csv_path = ctx.artifact("speed_calc.csv")
    if csv_path is not None:
        write_csv(csv_path, rows)
    return 0


def _run_gui(cfg: SpeedCalcConfig) -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as exc:
        log.error("当前 Python 环境缺少 tkinter，无法启动 GUI（可改用 --set speed_calc.mode=cli）: %s", exc)
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        log.error("GUI 进程启动失败（无图形界面？可改用 --set speed_calc.mode=cli）: %s", exc)
        return 1

    try:
        root.title("固件车速工程计算器")
        root.geometry("400x280")
        root.resizable(False, False)
        frame = ttk.Frame(root, padding="20 20 20 20")
        frame.pack(fill=tk.BOTH, expand=True)

        variables: list[tk.StringVar] = []
        rows = [
            ("电机转速 (RPM):", cfg.rpm),
            ("减速箱减速比:", cfg.gear_ratio),
            ("轮胎外径 (英寸):", cfg.wheel_diameter_inch),
        ]
        for i, (text, value) in enumerate(rows):
            var = tk.StringVar(value=f"{value:g}")
            ttk.Label(frame, text=text).grid(row=i, column=0, sticky=tk.W, pady=10)
            ttk.Entry(frame, textvariable=var, width=18).grid(row=i, column=1, sticky=tk.E, pady=10)
            variables.append(var)

        result_label = ttk.Label(frame, text="计算结果: \n-- km/h\n-- mp/h",
                                 font=("Arial", 14, "bold"), justify=tk.CENTER)
        result_label.grid(row=3, column=0, columnspan=2, pady=30)

        def _recalc(*_args: Any) -> None:
            try:
                text, result = evaluate_text_inputs(*(v.get() for v in variables), decimals=cfg.decimals)
            except Exception:  # noqa: BLE001 - GUI 回调兜底，避免异常打断实时输入
                log.exception("发生未知的核心计算异常")
                text, result = "系统计算异常，请查看日志", None
            result_label.config(text=text)
            if result is not None:
                log.info("自动计算成功 | RPM=%s, Ratio=%s, Dia=%s -> %.2f km/h, %.2f mp/h",
                         result.motor_rpm, result.gear_ratio, result.wheel_diameter_inch,
                         result.speed_kmh, result.speed_mph)

        for var in variables:
            var.trace_add("write", _recalc)
        log.info("GUI 界面加载完毕，已开启自动计算监听。")
        _recalc()
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass  # mainloop 正常退出时窗口已销毁
    return 0
