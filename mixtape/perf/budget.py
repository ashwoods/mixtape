"""Budgets for a test's numbers, from `@pytest.mark.perf(pipeline_latency_p95="20ms")`: the
keywords are a metric (its name with `_` for `.`), a statistic and a limit that the worst
subject of the metric must not pass."""

import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from .aggregate import RunStat
from .index import measure
from .metrics import METRICS

STATS = tuple(RunStat.model_fields)
"""The statistics of `summary.json`: n, mean, p50, p95, p99 and max"""

_TIMES = {"ns": 1.0, "us": 1e3, "µs": 1e3, "μs": 1e3, "ms": 1e6, "s": 1e9}  # to nanoseconds
_LIMIT = re.compile(r"(-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)\s*(ns|[uµμ]s|ms|s|%)", re.IGNORECASE)


class Budget(NamedTuple):
    metric: str
    stat: str
    limit: float
    """In the unit of the metric, as `summary.json` has it"""


def parse(keywords: Mapping[str, Any]) -> list[Budget]:
    """The budgets of the keywords of a `perf` marker. A ValueError names the keyword that is
    not a metric, a statistic or a limit the metric can have."""
    return [_budget(keyword, value) for keyword, value in keywords.items()]


def _budget(keyword: str, value: Any) -> Budget:
    name, _, stat = keyword.rpartition("_")
    metric = name.replace("_", ".")
    if stat not in STATS:
        raise ValueError(f"{keyword}: unknown statistic {stat!r}, use {', '.join(STATS)}")
    if metric not in METRICS:
        known = ", ".join(sorted(m.replace(".", "_") for m in METRICS))
        raise ValueError(f"{keyword}: unknown metric {name!r}, use {known}")
    unit = METRICS[metric].unit
    if isinstance(value, int | float) and not isinstance(value, bool):
        return Budget(metric, stat, float(value))
    if not isinstance(value, str) or (found := _LIMIT.fullmatch(value.strip())) is None:
        raise ValueError(
            f"{keyword}: {value!r} is not a limit: a number in the unit of the metric ({unit}) "
            "or a string with a unit (ns, us, ms, s or %), like '20ms'"
        )
    number, suffix = float(found[1]), found[2].lower()
    if stat == "n":
        raise ValueError(f"{keyword}: {value!r} counts samples, so it takes a number, no unit")
    if suffix == "%" and unit in ("%", "ratio"):
        return Budget(metric, stat, number / 100 if unit == "ratio" else number)
    if suffix != "%" and unit == "ns":
        return Budget(metric, stat, number * _TIMES[suffix])
    raise ValueError(f"{keyword}: {value!r} has a unit that {metric} ({unit}) does not take")


def _show(metric: str, stat: str, value: float) -> str:
    return f"{value:g}" if stat == "n" else measure(value, METRICS[metric].unit)


def check(budgets: list[Budget], stats: Mapping[str, Mapping[str, RunStat]]) -> list[str]:
    """What the budgets say of the statistics of a run (by metric and subject), one message for
    each that is over its limit: the metric, the statistic, the subject that has the worst of it,
    its value and the limit. A metric the run has no samples of is a message too."""
    messages = []
    for metric, stat, limit in budgets:
        subjects = stats.get(metric)
        if not subjects:
            messages.append(f"perf budget: {metric} {stat} was not measured, it has no samples")
            continue
        subject, worst = max(subjects.items(), key=lambda item: getattr(item[1], stat))
        value = getattr(worst, stat)
        if value > limit:
            messages.append(
                f"perf budget: {metric} {stat} is {_show(metric, stat, value)} on {subject}, "
                f"over {_show(metric, stat, limit)}"
            )
    return messages
