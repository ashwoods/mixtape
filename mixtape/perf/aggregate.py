"""Windows of samples and their statistics, for the live summary and the artifacts alike."""

import math
from array import array
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel

from .metrics import Sample


class Stat(BaseModel):
    """The samples of one metric and subject in a window. A rate metric has one sample per
    window, its per-second value"""

    n: int
    mean: float
    p50: float
    p95: float
    max: float
    last: float
    """The value of the latest sample"""


class Window(BaseModel):
    """The statistics of the samples a window collected, by metric and subject"""

    start: int
    """Nanoseconds since GStreamer initialised"""
    end: int
    metrics: dict[str, dict[str, Stat]]


class RunStat(BaseModel):
    """The samples of one metric and subject over a whole run"""

    n: int
    mean: float
    p50: float
    p95: float
    p99: float
    max: float


def percentile(ordered: Sequence[float], q: float) -> float:
    """The `q` (0 to 1) percentile of sorted values, interpolated between the two closest
    ranks: the median of an even number of values is the mean of the middle two."""
    rank = (len(ordered) - 1) * q
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _round(value: float) -> float:
    return round(value, 3)


def window(samples: Iterable[Sample], start: int, end: int) -> Window:
    """The window from `start` to `end` (ns) of the samples it collected."""
    groups: dict[tuple[str, str], list[Sample]] = defaultdict(list)
    for sample in samples:
        groups[sample.metric, sample.subject].append(sample)
    metrics: dict[str, dict[str, Stat]] = {}
    for (metric, subject), group in sorted(groups.items()):
        ordered = sorted(s.value for s in group)
        metrics.setdefault(metric, {})[subject] = Stat(
            n=len(ordered),
            mean=_round(math.fsum(ordered) / len(ordered)),
            p50=_round(percentile(ordered, 0.5)),
            p95=_round(percentile(ordered, 0.95)),
            max=_round(ordered[-1]),
            last=_round(max(group, key=lambda s: s.t_ns).value),
        )
    return Window(start=start, end=end, metrics=metrics)


def rates(counts: Mapping[str, tuple[int, int]], start: int, end: int) -> list[Sample]:
    """`pad.buffers` and `pad.bitrate` over a window from the buffers and bytes per pad."""
    seconds = (end - start) / 1e9
    if seconds <= 0:
        return []
    return [
        sample
        for pad, (buffers, size) in counts.items()
        for sample in (
            Sample(end, "pad.buffers", pad, buffers / seconds),
            Sample(end, "pad.bitrate", pad, size * 8 / seconds),
        )
    ]


class Totals:
    """Every value of a run by metric and subject, for its statistics at the end."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], array[float]] = {}

    def add(self, samples: Iterable[Sample]) -> None:
        for sample in samples:
            key = sample.metric, sample.subject
            if (values := self._values.get(key)) is None:
                values = self._values[key] = array("d")
            values.append(sample.value)

    def stats(self) -> dict[str, dict[str, RunStat]]:
        result: dict[str, dict[str, RunStat]] = {}
        for (metric, subject), values in sorted(self._values.items()):
            ordered = sorted(values)
            result.setdefault(metric, {})[subject] = RunStat(
                n=len(ordered),
                mean=_round(math.fsum(ordered) / len(ordered)),
                p50=_round(percentile(ordered, 0.5)),
                p95=_round(percentile(ordered, 0.95)),
                p99=_round(percentile(ordered, 0.99)),
                max=_round(ordered[-1]),
            )
        return result
