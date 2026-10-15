"""时间差计算工具（迁移自 Tool/时间差计算.py）。

计算两个时间字符串之间相差的小时数（结束早于起始时结果为负）。

运行::

    ppx-test run time_diff                                     # GUI（需要 tkinter）
    ppx-test run time_diff --set time_diff.mode=cli \\
        --set "time_diff.start=2026/4/8 16:45:28" --set "time_diff.end=2026/4/9 2:49:39"
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ppx_testkit.core.report.writers import write_csv
from ppx_testkit.exceptions import ConfigError
from ppx_testkit.logger import RunContext, get_logger
from ppx_testkit.settings import AppSettings

log = get_logger(__name__)

DEFAULT_FORMATS: tuple[str, ...] = ("%Y/%m/%d %H:%M:%S",)
FORMAT_HINT = "输入的时间格式不正确，请确保格式类似于：2026/4/8 16:45:28"


# ============================================================ 纯计算
@dataclass(frozen=True)
class TimeDiffResult:
    start: datetime
    end: datetime
    seconds: float

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    def to_dict(self, decimals: int = 4) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(sep=" "),
            "end": self.end.isoformat(sep=" "),
            "seconds": self.seconds,
            "hours": round(self.hours, decimals),
        }


def parse_time(text: str, formats: Sequence[str] = DEFAULT_FORMATS) -> datetime:
    """按 formats 顺序尝试解析，全部失败抛出 ValueError（附格式提示）。"""
    value = text.strip()
    if not value:
        raise ValueError("时间不能为空")
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"{FORMAT_HINT}（实际输入: {value!r}，支持格式: {list(formats)}）")


def time_diff(start_text: str, end_text: str, formats: Sequence[str] = DEFAULT_FORMATS) -> TimeDiffResult:
    start = parse_time(start_text, formats)
    end = parse_time(end_text, formats)
    return TimeDiffResult(start=start, end=end, seconds=(end - start).total_seconds())


def time_diff_hours(start_text: str, end_text: str, formats: Sequence[str] = DEFAULT_FORMATS) -> float:
    return time_diff(start_text, end_text, formats).hours


def format_hours(hours: float, decimals: int = 4) -> str:
    return f"时间差: {hours:.{decimals}f} 小时"


# ============================================================ 配置
@dataclass(frozen=True)
class TimePair:
    start: str
    end: str
    name: str = ""


@dataclass(frozen=True)
class TimeDiffConfig:
    mode: Literal["gui", "cli"] = "gui"
    formats: list[str] = field(default_factory=lambda: list(DEFAULT_FORMATS))
    decimals: int = 4
    start: str | None = None
    end: str | None = None
    pairs: list[TimePair] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.formats:
            raise ConfigError("time_diff.formats 不能为空")
        if not 0 <= self.decimals <= 10:
            raise ConfigError(f"time_diff.decimals 应在 0~10 之间，实际 {self.decimals}")
        if bool(self.start) != bool(self.end):
            raise ConfigError("time_diff.start 与 time_diff.end 必须同时配置")
        for i, pair in enumerate(self.all_pairs()):
            for label, text in (("start", pair.start), ("end", pair.end)):
                try:
                    parse_time(text, self.formats)
                except ValueError as exc:
                    raise ConfigError(f"time_diff 第 {i + 1} 组 {label}: {exc}") from exc

    def all_pairs(self) -> list[TimePair]:
        head = [TimePair(start=self.start, end=self.end, name="默认")] if self.start and self.end else []
        return [*head, *self.pairs]


# ============================================================ 入口
def run(settings: AppSettings, ctx: RunContext) -> int:
    cfg = settings.section("time_diff", TimeDiffConfig, required=False)
    if cfg.mode == "gui":
        return _run_gui(cfg)
    return run_cli(cfg, ctx)


def run_cli(cfg: TimeDiffConfig, ctx: RunContext) -> int:
    pairs = cfg.all_pairs()
    if not pairs:
        raise ConfigError("cli 模式需配置 time_diff.start/end 或 time_diff.pairs")
    rows: list[dict[str, Any]] = []
    for pair in pairs:
        r = time_diff(pair.start, pair.end, cfg.formats)
        log.info("[%s] 成功计算时间差: %s 至 %s，差值: %.*f 小时",
                 pair.name or "-", pair.start, pair.end, cfg.decimals, r.hours)
        rows.append({"name": pair.name, **r.to_dict(cfg.decimals)})
    ctx.write_json("time_diff.json", rows)
    csv_path = ctx.artifact("time_diff.csv")
    if csv_path is not None:
        write_csv(csv_path, rows)
    return 0


def _run_gui(cfg: TimeDiffConfig) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError as exc:
        log.error("当前 Python 环境缺少 tkinter，无法启动 GUI（可改用 --set time_diff.mode=cli）: %s", exc)
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        log.error("应用程序启动失败（无图形界面？可改用 --set time_diff.mode=cli）: %s", exc)
        return 1

    try:
        root.title("时间差计算工具")
        root.geometry("450x300")
        root.resizable(False, False)

        tk.Label(root, text="起始时间 (例: 2026/4/8 16:45:28):", font=("Arial", 10)).pack(pady=(20, 5))
        start_entry = tk.Entry(root, width=40, font=("Arial", 10))
        start_entry.pack()
        tk.Label(root, text="结束时间 (例: 2026/4/9 2:49:39):", font=("Arial", 10)).pack(pady=(15, 5))
        end_entry = tk.Entry(root, width=40, font=("Arial", 10))
        end_entry.pack()
        if cfg.start and cfg.end:
            start_entry.insert(0, cfg.start)
            end_entry.insert(0, cfg.end)

        result_label = tk.Label(root, text="等待输入计算...", font=("Arial", 12, "bold"), fg="blue")

        def _on_calculate() -> None:
            start_text, end_text = start_entry.get().strip(), end_entry.get().strip()
            if not start_text or not end_text:
                log.warning("用户尝试在输入为空的情况下进行计算。")
                messagebox.showwarning("输入警告", "起始时间和结束时间不能为空，请填写完整。")
                return
            try:
                hours = time_diff_hours(start_text, end_text, cfg.formats)
            except ValueError as exc:
                log.error("时间解析错误: %s", exc)
                messagebox.showerror("格式错误", str(exc))
                result_label.config(text="计算失败：格式错误", fg="red")
                return
            except Exception as exc:  # noqa: BLE001 - GUI 回调兜底
                log.exception("计算过程中发生未知错误")
                messagebox.showerror("系统错误", str(exc))
                result_label.config(text="计算失败：系统错误", fg="red")
                return
            log.info("成功计算时间差: %s 至 %s，差值: %.*f 小时", start_text, end_text, cfg.decimals, hours)
            result_label.config(text=format_hours(hours, cfg.decimals), fg="green")

        tk.Button(root, text="计算时间差 (小时)", command=_on_calculate, width=20, bg="lightgray").pack(pady=25)
        result_label.pack(pady=10)
        log.info("应用程序界面初始化完成。")
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass  # mainloop 正常退出时窗口已销毁
    return 0
