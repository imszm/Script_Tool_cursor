"""结果报告输出（CSV / HTML）。"""

from __future__ import annotations

import csv
import html
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from string import Template
from typing import Any

log = logging.getLogger(__name__)

_TEMPLATE = Path(__file__).with_name("templates") / "report.html"


_WINDOW_MARK = "当时可见窗口："

# 执行器摘要用英文键。页面上换成短中文，长文本不跟数字挤在同一行。
_LABELS: dict[str, str] = {
    "station": "工位",
    "target_cycles": "目标轮次",
    "executed": "已执行",
    "passed": "通过",
    "failed": "失败",
    "abort_reason": "熔断原因",
    "keep_power": "保留现场",
    "error": "程序异常",
    "started_at": "开始",
    "ended_at": "结束",
    "duration_s": "耗时",
    "pass_rate": "通过率",
    "目标轮数": "目标轮次",
}
_STAT_KEYS = (
    "target_cycles",
    "目标轮数",
    "executed",
    "已执行",
    "执行/目标",
    "passed",
    "通过",
    "failed",
    "失败",
    "通过/失败",
    "pass_rate",
    "通过率",
    "用例总数",
    "退出码",
)
_NOTE_KEYS = {"abort_reason", "error", "熔断原因", "程序异常"}
_COLUMN_LABELS = {
    "cycle": "轮次",
    "index": "序号",
    "verdict": "结果",
    "detail": "说明",
    "details": "说明",
    "duration_s": "耗时(s)",
    "name": "项目",
    "timestamp": "时间",
}


def _cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _label(key: str) -> str:
    """摘要字段在页面上的名字。没有对照时沿用原键，其它工位的中文键因此保持不变。"""
    return _LABELS.get(key, key)


def _format_plain(key: str, value: Any) -> str:
    """把数字和布尔收成适合卡片的短文本。

    :param key: 摘要字段名，用来决定百分号和秒。
    :param value: 原始值。
    :return: 已适合直接显示的字符串，尚未做 HTML 转义。
    """
    if isinstance(value, bool):
        return "是" if value else "否"
    if key in ("pass_rate", "通过率") and isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.2f}%"
    if key == "duration_s" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.1f} 秒"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return _cell(value)


def _split_windows(text: str) -> tuple[str, list[str], int | None]:
    """把「当时可见窗口」从失败原因里拆出来。

    :param text: 熔断原因或表格说明。
    :return: ``(原因正文, 列出的窗口, 总数)``。没有这段时窗口列表为空。
    """
    if _WINDOW_MARK not in text:
        return text, [], None
    head, _, tail = text.partition(_WINDOW_MARK)
    tail = tail.strip().removesuffix("。")
    total: int | None = None
    if " 等 " in tail:
        listed, _, rest = tail.partition(" 等 ")
        digits = "".join(ch for ch in rest if ch.isdigit())
        total = int(digits) if digits else None
    else:
        listed = tail
    names = [part.strip() for part in listed.split("、") if part.strip()]
    return head.strip(), names, total


def _render_prose(text: str) -> str:
    """失败原因单独成段。窗口标题放进可展开列表，避免一行撑满整页。

    :param text: 原始说明。
    :return: 已转义的 HTML。
    """
    head, names, total = _split_windows(text)
    if not names:
        return f"<p>{html.escape(text)}</p>"
    count = total if total is not None else len(names)
    items = "".join(f"<li>{html.escape(name)}</li>" for name in names)
    paragraphs = f"<p>{html.escape(head)}</p>" if head else ""
    return (
        f"{paragraphs}<details><summary>当时可见窗口（{count}）</summary>"
        f'<ul class="windows">{items}</ul></details>'
    )


def _status_badge(summary: Mapping[str, Any]) -> str:
    """根据结论生成标题右侧的状态。没有明确结论时返回空串。

    :param summary: 报告摘要。
    :return: 徽标 HTML。
    """
    if summary.get("aborted") is True:
        return '<span class="badge fail">熔断</span>'
    if summary.get("interrupted") is True:
        return '<span class="badge warn">中断</span>'
    conclusion = summary.get("结论")
    if summary.get("ok") is True or conclusion == "PASS":
        return '<span class="badge pass">通过</span>'
    if summary.get("ok") is False or conclusion == "FAIL":
        return '<span class="badge fail">未通过</span>'
    failed = summary.get("失败")
    passed = summary.get("通过")
    if isinstance(failed, int) and isinstance(passed, int):
        if failed == 0:
            return '<span class="badge pass">通过</span>'
        return '<span class="badge fail">有失败</span>'
    return ""


def _summary_items(summary: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """展开 extra，并去掉已经体现在状态徽标上的字段。

    :param summary: 报告摘要。
    :return: 仍需显示的 ``(字段, 值)``。
    """
    items: list[tuple[str, Any]] = []
    for key, value in summary.items():
        if key == "extra" and isinstance(value, dict):
            items.extend((str(name), item) for name, item in value.items())
            continue
        items.append((str(key), value))
    hidden = {"ok", "aborted", "interrupted", "结论"}
    visible: list[tuple[str, Any]] = []
    for key, value in items:
        if key in hidden or value is None or value == "" or value == {}:
            continue
        if key == "keep_power" and value is False:
            continue
        visible.append((key, value))
    return visible


def _render_summary(summary: Mapping[str, Any]) -> str:
    """摘要分成数字卡片、短信息和长说明三段。

    :param summary: 报告摘要。
    :return: 摘要区 HTML。空摘要返回空串。
    """
    stats: list[tuple[str, Any]] = []
    meta: list[tuple[str, Any]] = []
    notes: list[tuple[str, str]] = []
    for key, value in _summary_items(summary):
        text = _format_plain(key, value)
        if key in _NOTE_KEYS or len(text) > 42:
            notes.append((key, text if key not in _NOTE_KEYS else _cell(value)))
            continue
        if key in _STAT_KEYS:
            stats.append((key, value))
            continue
        meta.append((key, value))
    order = {name: index for index, name in enumerate(_STAT_KEYS)}
    stats.sort(key=lambda item: order.get(item[0], len(order)))

    parts: list[str] = []
    if stats:
        cards = []
        for key, value in stats:
            tone = ""
            if key in ("failed", "失败") and isinstance(value, int) and value > 0:
                tone = " bad"
            elif key in ("passed", "通过") and isinstance(value, int) and value > 0:
                tone = " good"
            cards.append(
                f'<div class="stat{tone}"><div class="label">{html.escape(_label(key))}</div>'
                f'<div class="value">{html.escape(_format_plain(key, value))}</div></div>'
            )
        parts.append(f'<section class="stats">{"".join(cards)}</section>')
    if meta:
        chips = "".join(
            f"<span><b>{html.escape(_label(key))}</b>{html.escape(_format_plain(key, value))}</span>"
            for key, value in meta
        )
        parts.append(f'<div class="meta">{chips}</div>')
    if notes:
        blocks = "".join(
            f'<article class="note"><h2>{html.escape(_label(key))}</h2>{_render_prose(text)}</article>'
            for key, text in notes
        )
        parts.append(f'<section class="notes">{blocks}</section>')
    return "".join(parts)


def _column_class(name: str) -> str:
    """表格列的宽度类别。

    :param name: 列名。
    :return: ``num``、``verdict``、``detail`` 或空串。
    """
    if name in ("cycle", "index"):
        return "num"
    if name == "verdict":
        return "verdict"
    if name in ("detail", "details"):
        return "detail"
    return ""


def _render_cell(column: str, value: Any) -> str:
    """渲染一个单元格。结果列用色标，失败说明里的窗口列表默认收起。

    :param column: 列名。
    :param value: 单元格原值。
    :return: 已转义的 HTML。
    """
    if column == "verdict" and isinstance(value, str) and value.upper() in ("PASS", "FAIL"):
        kind = "pass" if value.upper() == "PASS" else "fail"
        label = "通过" if kind == "pass" else "失败"
        return f'<span class="pill {kind}">{label}</span>'
    text = _cell(value)
    if column in ("detail", "details"):
        if not text:
            return '<span class="empty">—</span>'
        if _WINDOW_MARK in text:
            return _render_prose(text)
    return html.escape(text)


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str] | None = None) -> Path | None:
    columns = list(columns or (rows[0].keys() if rows else []))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _cell(row.get(k)) for k in columns})
        log.info("CSV 报告已生成: %s", path)
        return path
    except OSError:
        log.exception("写入 CSV 报告失败: %s", path)
        return None


def write_html(
    path: Path,
    *,
    title: str,
    summary: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str] | None = None,
    verdict_key: str = "verdict",
) -> Path | None:
    """写成单页 HTML。数字进卡片，熔断原因单独成段，窗口列表默认收起。

    :param path: 输出路径。
    :param title: 页面标题。
    :param summary: 摘要字段。英文键会换成中文标签。
    :param rows: 明细行。
    :param columns: 列顺序。空则取第一行的键。
    :param verdict_key: 判定列名，用来筛选和上色。
    :return: 写入成功时的路径。目录创建失败时返回 None。
    """
    columns = list(columns or (rows[0].keys() if rows else []))
    head = "".join(
        f'<th class="{_column_class(name)}">{html.escape(_COLUMN_LABELS.get(name, name))}</th>'
        for name in columns
    )
    body_rows = []
    for row in rows:
        verdict = str(row.get(verdict_key, "")).upper()
        cls = "pass" if verdict == "PASS" else ("fail" if verdict == "FAIL" else "")
        cells = "".join(
            f'<td class="{_column_class(name)}">{_render_cell(name, row.get(name))}</td>' for name in columns
        )
        body_rows.append(f'<tr class="{cls}" data-verdict="{html.escape(verdict)}">{cells}</tr>')
    try:
        template = Template(_TEMPLATE.read_text(encoding="utf-8"))
        content = template.safe_substitute(
            title=html.escape(title),
            badge=_status_badge(summary),
            summary=_render_summary(summary),
            count=f"共 {len(rows)} 条",
            head=head,
            body="\n".join(body_rows),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.info("HTML 报告已生成: %s", path)
        return path
    except OSError:
        log.exception("写入 HTML 报告失败: %s", path)
        return None


def write_detail_reports(
    ctx: Any,
    name: str,
    *,
    title: str,
    rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    columns: Sequence[str] | None = None,
    html_report: bool = True,
    verdict_key: str = "verdict",
    summary_name: str | None = None,
) -> None:
    """把明细写成 CSV，并按需写成 HTML。``ctx`` 需要 run_dir 与 artifact。

    ``summary_name`` 非空时另外写入 JSON 摘要。运行目录不可用时直接返回。
    """
    if ctx is None or getattr(ctx, "run_dir", None) is None:
        return
    csv_path = ctx.artifact(f"{name}.csv")
    if csv_path is not None:
        write_csv(csv_path, rows, columns)
    if html_report:
        html_path = ctx.artifact(f"{name}.html")
        if html_path is not None:
            write_html(
                html_path,
                title=title,
                summary=summary,
                rows=rows,
                columns=columns,
                verdict_key=verdict_key,
            )
    if summary_name is not None:
        ctx.write_json(summary_name, dict(summary))
