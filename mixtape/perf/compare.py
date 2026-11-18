"""Compare two runs of the perf measuring, to see what got slower: two sessions of the pytest
plugin (folders with an `index.json`) or two single runs (folders with a `summary.json`).

Only the metrics where a higher value is worse and that are steady enough (`Metric.compared`)
are compared, and by their p95 and max: a session by the worst of each test's subjects, a run
by each subject. A value is a regression when it grew by more than `threshold` of the base
*and* by more than the floor of its unit, so that a big relative change of something tiny (a
latency of 50 µs that reads 90 µs) is not one; an improvement is the same, the other way."""

import json
from pathlib import Path
from typing import Any

from .index import measure
from .metrics import METRICS
from .output import FORMAT_VERSION

THRESHOLD = 0.5
"""How much a value may grow over the base, as a fraction of it, before it is a regression.
At 25% two runs of the same code flagged the CPU of short tests (`tests/test_player.py`, 27% to
37% over)"""

FLOORS = {"ns": 100_000.0, "%": 5.0, "ratio": 0.05, "buffers": 1.0, "bytes": 4096.0}
"""How much a value must change in absolute terms to count, by unit (the unit's own: 100 µs, 5
percentage points, 5% of a queue, one buffer, a page); the one place to tune them"""


STATS = ("p95", "max")


class CompareError(ValueError):
    """A folder is not something to compare."""


def kind(folder: Path) -> str:
    """`session` for a folder with an `index.json`, `run` for one with a `summary.json`."""
    for name, found in (("index.json", "session"), ("summary.json", "run")):
        if (folder / name).is_file():
            return found
    if not folder.exists():
        raise CompareError(f"{folder} does not exist")
    raise CompareError(
        f"{folder} has neither an index.json nor a summary.json: `mixtape run --feature "
        "perf,out=DIR` writes a summary.json, `pytest --mixtape-perf DIR` an index.json"
    )


def _values(document: dict[str, Any], what: str) -> dict[tuple[str, ...], dict[str, Any]]:
    """The numbers to compare of an `index.json` (`what` is `session`) or a `summary.json`,
    by (test, metric) or (metric, subject): `{"unit", "p95": (value, subject), "max": …}`."""
    found: dict[tuple[str, ...], dict[str, Any]] = {}
    if what == "session":
        for test in document["tests"]:
            for metric, worst in test["metrics"].items():
                # a metric of a newer version is not known: skipped
                if metric in METRICS and METRICS[metric].compared:
                    found[test["id"], metric] = {
                        "unit": worst["unit"],
                        **{stat: (worst[stat]["value"], worst[stat]["subject"]) for stat in STATS},
                    }
    else:
        for metric, entry in document["metrics"].items():
            if metric in METRICS and METRICS[metric].compared:
                for subject, stats in entry["subjects"].items():
                    found[metric, subject] = {
                        "unit": entry["unit"],
                        **{stat: (stats[stat], subject) for stat in STATS},
                    }
    return found


def _tests(document: dict[str, Any], what: str) -> set[str]:
    return {test["id"] for test in document["tests"]} if what == "session" else set()


def _lost(document: dict[str, Any], what: str, side: str) -> list[dict[str, Any]]:
    """The tests (`{"side", "test", "lost"}`) or the run (`{"side", "lost"}`) of one side
    whose debug log dropped lines, so some of their numbers are missing."""
    if what == "session":
        return [
            {"side": side, "test": test["id"], "lost": test["lost"]}
            for test in document["tests"]
            if test.get("lost")
        ]
    return [{"side": side, "lost": document["lost"]}] if document.get("lost") else []


def _missing(
    mine: dict[tuple[str, ...], Any],
    tests: set[str],
    lack: dict[tuple[str, ...], Any],
    gone: set[str],
    what: str,
) -> list[dict[str, str]]:
    """What one side has and the other (with its `gone` tests) lacks: the tests, one object each
    and not one for each of their metrics, then the metrics (and subjects) of the others."""
    names: list[dict[str, str]] = [{"test": test} for test in sorted(tests - gone)]
    fields: tuple[str, ...] = ("test", "metric") if what == "session" else ("metric", "subject")
    names += [
        dict(zip(fields, key, strict=True))
        for key in mine
        if key not in lack and (not tests or key[0] in gone)
    ]
    return names


def compare(
    base: dict[str, Any], new: dict[str, Any], what: str, threshold: float = THRESHOLD
) -> dict[str, Any]:
    """Compare two `index.json` (`what` is `session`) or two `summary.json` (`run`) documents.
    `regressions` and `improvements` are lists of `{"test" (sessions), "metric", "subject",
    "stat", "unit", "base", "new"}`, the worst change first; `added` and `removed` are lists of
    what only one side has, as objects with those keys that say it: `{"test"}`, `{"test",
    "metric"}` or, for a run, `{"metric", "subject"}`. `lost` lists the tests, or the runs, of
    either side whose debug log dropped lines (see `_lost`): their numbers are incomplete.
    `threshold_percent` is `threshold` in percent, as `--threshold` is given."""
    before, after = _values(base, what), _values(new, what)
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    both = sorted(before.keys() & after.keys())
    for key in both:
        for stat in STATS:
            (was, _), (now, subject) = before[key][stat], after[key][stat]
            unit = after[key]["unit"]
            change = {
                **({"test": key[0], "metric": key[1]} if what == "session" else {"metric": key[0]}),
                "subject": subject if what == "session" else key[1],
                "stat": stat,
                "unit": unit,
                "base": was,
                "new": now,
            }
            least = FLOORS.get(unit, 0.0)
            if now - was > max(threshold * abs(was), least):
                regressions.append(change)
            elif was - now > max(threshold * abs(now), least):
                improvements.append(change)

    def size(change: dict[str, Any]) -> float:  # how big it is, in floors of its unit
        return abs(change["new"] - change["base"]) / (FLOORS.get(change["unit"]) or 1)

    regressions.sort(key=size, reverse=True)
    improvements.sort(key=size, reverse=True)
    tests_before, tests_after = _tests(base, what), _tests(new, what)
    return {
        "kind": what,
        "threshold_percent": round(threshold * 100, 9),
        "regressions": regressions,
        "improvements": improvements,
        "added": _missing(after, tests_after, before, tests_before, what),
        "removed": _missing(before, tests_before, after, tests_after, what),
        "lost": _lost(base, what, "base") + _lost(new, what, "new"),
        "compared": len(both) * len(STATS),
    }


def load(folder: Path) -> tuple[dict[str, Any], str]:
    """The `index.json` or `summary.json` of a folder, and which of them it is. One of a
    newer `format_version`, or without the fields to compare, is a `CompareError`."""
    what = kind(folder)
    path = folder / ("index.json" if what == "session" else "summary.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CompareError(f"cannot read {path}: {error}") from None
    version = document.get("format_version", 1) if isinstance(document, dict) else 1
    if version != FORMAT_VERSION:
        raise CompareError(
            f"{path} has format_version {version!r}, and this mixtape reads "
            f"{FORMAT_VERSION}: compare it with the mixtape that wrote it"
        )
    try:
        _values(document, what)
        _lost(document, what, "")
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise CompareError(
            f"cannot read {path}: it is not as mixtape writes it ({type(error).__name__}: {error})"
        ) from None
    return document, what


def compare_folders(base: Path, new: Path, threshold: float = THRESHOLD) -> dict[str, Any]:
    """Compare two folders, both sessions or both runs."""
    (first, first_kind), (second, second_kind) = load(base), load(new)
    if first_kind != second_kind:
        raise CompareError(
            f"{base} is a {first_kind} (it has an "
            f"{'index' if first_kind == 'session' else 'summary'}.json) but {new} is a "
            f"{second_kind}: compare two sessions or two runs"
        )
    return {
        "base": str(base),
        "new": str(new),
        **compare(first, second, first_kind, threshold),
    }


def _change(change: dict[str, Any]) -> list[str]:
    unit = change["unit"]
    was, now = change["base"], change["new"]
    shown = f"{measure(was, unit)} → {measure(now, unit)}"
    percent = f"{(now - was) / abs(was) * 100:+.0f}%" if was else "new"
    if percent == "-100%" and now:  # -100% is for a value that fell to 0
        percent = "-99%"
    where = [change["test"]] if "test" in change else []
    return [*where, change["metric"], change["stat"], change["subject"], shown, percent]


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return [
        "  " + "  ".join([*(c.ljust(w) for c, w in zip(row, widths, strict=False)), row[-1]])
        for row in rows
    ]


def format_text(result: dict[str, Any]) -> str:
    """The comparison as a plain table of regressions, then improvements, then added and
    removed, then a line of counts."""
    lines: list[str] = []
    for title, key in (("Regressions", "regressions"), ("Improvements", "improvements")):
        if result[key]:
            lines += [f"{title}:", *_table([_change(c) for c in result[key]]), ""]
    for title, key in (("Added", "added"), ("Removed", "removed")):
        if result[key]:
            lines += [f"{title}:", *("  " + "  ".join(item.values()) for item in result[key]), ""]
    if lost := result.get("lost"):
        rows = [
            [item["side"], *([item["test"]] if "test" in item else []), str(item["lost"])]
            for item in lost
        ]
        lines += [
            "Lost log lines (times; some tracer records are missing from these numbers):",
            *_table(rows),
            "",
        ]
    lines.append(
        f"{len(result['regressions'])} regressions, {len(result['improvements'])} improvements, "
        f"{len(result['added'])} added, {len(result['removed'])} removed "
        f"(threshold {result['threshold_percent']:g}%, {result['compared']} values compared)"
    )
    return "\n".join(lines)
