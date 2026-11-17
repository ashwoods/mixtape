"""`report.html`: a run's artifacts as one page that works offline, from a file, for good.

It inlines all it needs: uPlot, the styles and the graph and chart code of the control page
with the report's own (`mixtape/interfaces/http/report.js`), and the run: `summary.json`, with
the graph `perf` keeps, and `windows.jsonl`. Its content security policy lets it fetch
nothing."""

import html
import importlib.resources
import json
from pathlib import Path
from typing import Any

UPLOT_BANNER = "/*! https://github.com/leeoniya/uPlot (v1.6.32) */"

POLICY = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"


def _read(path: str) -> str:
    return importlib.resources.files("mixtape").joinpath(path).read_text(encoding="utf-8")


def _inline(code: str) -> str:
    """`code` safe inside a script or style element: nothing in it can close one."""
    return code.replace("</", "<\\/")


def write(folder: Path) -> Path:
    """Write `folder`/report.html from the `summary.json` and `windows.jsonl` there."""
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    with open(folder / "windows.jsonl", encoding="utf-8") as file:
        windows = [json.loads(line) for line in file if line.strip()]
    path = folder / "report.html"
    path.write_text(render(summary, windows), encoding="utf-8")
    return path


def render(summary: dict[str, Any], windows: list[dict[str, Any]]) -> str:
    """The page of a run."""
    # uPlot's banner names its home: keep the credit, without a URL on the page
    uplot = _read("vendor/uplot/uPlot.iife.min.js").replace(UPLOT_BANNER, "/*! uPlot v1.6.32 */")
    # every `<` escaped, not just `</`: a `<!--<script` in a name would keep the data's script
    # element open past its end tag, swallowing the code after it
    data = json.dumps({"summary": summary, "windows": windows}, separators=(",", ":"))
    data = data.replace("<", "\\u003c")
    styles = (_read("vendor/uplot/uPlot.min.css"), _read("interfaces/http/mixtape.css"))
    scripts = (
        _read("interfaces/http/graph.js"),
        uplot,
        _read("interfaces/http/perf.js"),
        _read("interfaces/http/report.js"),
    )
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f'<meta http-equiv="Content-Security-Policy" content="{POLICY}">',
            f"<title>perf · {html.escape(str(summary.get('pipeline') or 'run'))}</title>",
            *(f"<style>\n{_inline(css)}\n</style>" for css in styles),
            "</head>",
            "<body>",
            '<header class="report-head"><div id="head"></div></header>',
            '<main class="report-main" id="main"><noscript><p class="noscript">This report '
            "draws with JavaScript. Its numbers are in summary.json and windows.jsonl."
            "</p></noscript></main>",
            f'<script type="application/json" id="run">{data}</script>',
            *(f"<script>\n{_inline(code)}\n</script>" for code in scripts),
            "</body>",
            "</html>",
            "",
        ]
    )
