"""`index.html`: the tests of a pytest run (`mixtape.pytest_plugin`) on one page that works
offline, from a file: a table of each test's outcome and its worst numbers, linking to its
`report.html`. It is written once, without script, with the styles of the report; every
value is escaped as it goes in."""

import html
import importlib.resources
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .metrics import METRICS

POLICY = "default-src 'none'; style-src 'unsafe-inline'"

STYLE = """
.index { max-width: none; }
.index td.subject { max-width: 640px; }
.index .sub { color: var(--faint); margin-left: 6px; }
.index a { color: var(--accent); text-decoration: none; }
.index a:hover { text-decoration: underline; }
.index td.bad { color: var(--err); }
.index td.warn { color: var(--warn); }
.index td.off, .index td.none { color: var(--faint); }
"""

COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("pipeline.latency", "p95", "pipeline latency", "p95, the slowest source to sink"),
    ("element.latency", "p95", "element latency", "p95, the slowest element"),
    ("process.cpu", "mean", "cpu", "mean over the test; 100% is one core"),
    ("queue.fill", "max", "queue fill", "max, the fullest queue"),
)
"""The metric and statistic of each numeric column, with its title and what the title means"""

_SCALES = {
    "ns": ((1e9, " s"), (1e6, " ms"), (1e3, " µs"), (1, " ns")),
    "ratio": ((0.01, "%"),),
    "%": ((1, "%"),),
}


def measure(value: float, unit: str) -> str:
    """A measure in its unit with three significant digits, as the report shows them: 23.8 ms,
    53.1%."""
    steps = _SCALES.get(unit, ((1, f" {unit}"),))
    rounded = float(f"{value:.3g}")  # 999.6 ms reads 1.00 s
    divisor, suffix = next((s for s in steps if abs(rounded) >= s[0]), steps[-1])
    x = value / divisor
    digits = 0 if x == int(x) or abs(x) >= 100 else 1 if abs(x) >= 10 else 2
    return f"{x:.{digits}f}{suffix}"


def _read(path: str) -> str:
    return importlib.resources.files("mixtape").joinpath(path).read_text(encoding="utf-8")


def _cell(css: str, text: str, title: str | None = None) -> str:
    attr = f' class="{css}"' if css else ""
    if title is not None:
        attr += f' title="{html.escape(title)}"'
    return f"<td{attr}>{text}</td>"


def _numbers(test: dict[str, Any]) -> list[str]:
    cells = []
    for metric, stat, _, _ in COLUMNS:
        found = test["metrics"].get(metric, {}).get(stat)
        if found is None:
            cells.append(_cell("none", "—"))
            continue
        text = html.escape(measure(found["value"], METRICS[metric].unit))
        subject = found["subject"]
        if metric == "element.latency":  # the element is the news; a pipeline's has one subject
            text += f' <span class="sub">{html.escape(subject)}</span>'
        cells.append(_cell("", text, subject))
    return cells


def _row(test: dict[str, Any], lost: bool) -> str:
    name = html.escape(test["id"])
    if test["folder"] is not None:
        href = html.escape(quote(f"{test['folder']}/report.html"))
        name = f'<a href="{href}">{name}</a>'
    outcome = test["outcome"]
    paint = {"failed": "bad", "error": "bad", "skipped": "off", "xfailed": "off"}.get(outcome, "")
    cells = [
        _cell("subject", name, test["id"]),
        _cell(paint, html.escape(outcome)),
        _cell("", html.escape(measure(test["duration"] * 1e9, "ns"))),
        *_numbers(test),
    ]
    if lost:
        cells.append(_cell("warn" if test["lost"] else "none", str(test["lost"] or "—")))
    return "<tr>" + "".join(cells) + "</tr>"


def render(index: dict[str, Any]) -> str:
    """The page of an `index.json`."""
    tests: list[dict[str, Any]] = index["tests"]
    measured = sum(test["folder"] is not None for test in tests)
    lossy = [test for test in tests if test["lost"]]
    facts = [
        f"tests <b>{len(tests)}</b>",
        f"measured <b>{measured}</b>",
        f"windows of <b>{index['interval']:g} s</b>",
    ]
    if lossy:
        facts.append(f'lost log lines in <b class="warn">{len(lossy)}</b>')
    columns = [
        "<th>test</th>",
        "<th>outcome</th>",
        "<th>duration</th>",
        *(
            f'<th title="{html.escape(hint)}">{html.escape(title)}</th>'
            for _, _, title, hint in COLUMNS
        ),
    ]
    if lossy:
        columns.append(
            '<th title="times the debug log dropped lines before they were read">lost</th>'
        )
    rows = [_row(test, bool(lossy)) for test in tests]
    body = (
        f'<div class="stats-wrap"><table class="stats index"><thead><tr>{"".join(columns)}</tr>'
        f"</thead><tbody>{''.join(rows)}</tbody></table></div>"
        if rows
        else '<div class="empty">No test ran.</div>'
    )
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f'<meta http-equiv="Content-Security-Policy" content="{POLICY}">',
            "<title>perf · tests</title>",
            f"<style>\n{_read('interfaces/http/mixtape.css')}\n{STYLE}</style>",
            "</head>",
            "<body>",
            '<header class="report-head"><div>',
            '<div class="brand"><span class="app">mixtape</span>'
            '<span class="name">Perf in tests</span></div>',
            f'<ul class="facts">{"".join(f"<li>{fact}</li>" for fact in facts)}</ul>',
            "</div></header>",
            '<main class="report-main">',
            '<h2 class="sec-h">Tests <span class="n">in the order they ran · worst of the '
            "subjects of each metric</span></h2>",
            body,
            "</main>",
            "</body>",
            "</html>",
            "",
        ]
    )


def write(folder: Path, index: dict[str, Any]) -> None:
    """Write `index.json` and `index.html` to `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    (folder / "index.html").write_text(render(index), encoding="utf-8")
