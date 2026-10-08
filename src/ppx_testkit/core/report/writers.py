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


def _cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


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
    columns = list(columns or (rows[0].keys() if rows else []))
    head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    body_rows = []
    for row in rows:
        verdict = str(row.get(verdict_key, "")).upper()
        cls = "pass" if verdict == "PASS" else ("fail" if verdict == "FAIL" else "")
        cells = "".join(f"<td>{html.escape(_cell(row.get(c)))}</td>" for c in columns)
        body_rows.append(f'<tr class="{cls}" data-verdict="{html.escape(verdict)}">{cells}</tr>')
    summary_html = "".join(
        f"<div class='kv'><b>{html.escape(str(k))}</b><span>{html.escape(_cell(v))}</span></div>"
        for k, v in summary.items()
    )
    try:
        template = Template(_TEMPLATE.read_text(encoding="utf-8"))
        content = template.safe_substitute(
            title=html.escape(title), summary=summary_html, head=head, body="\n".join(body_rows)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.info("HTML 报告已生成: %s", path)
        return path
    except OSError:
        log.exception("写入 HTML 报告失败: %s", path)
        return None
