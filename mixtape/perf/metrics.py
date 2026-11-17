"""What `perf` measures: the samples and the fixed table of metrics they belong to."""

import json
from typing import NamedTuple


class Sample(NamedTuple):
    """One measurement: at `t_ns` (nanoseconds since GStreamer initialised, the clock of its
    tracers), `metric` of `subject` (an element, `element:pad`, `src→sink`, `process` or a
    thread's name) had `value`, in the unit of the metric."""

    t_ns: int
    metric: str
    subject: str
    value: float


class Metric(NamedTuple):
    unit: str
    description: str
    higher_is_worse: bool = False
    """Whether a higher value is a regression: what `perf compare` looks at. Rates and the
    interval between buffers say what the pipeline does, not how well"""


METRICS: dict[str, Metric] = {
    "element.latency": Metric(
        "ns",
        "Time a buffer takes through an element, sink pad to source pad (core latency)",
        higher_is_worse=True,
    ),
    "pipeline.latency": Metric(
        "ns",
        "Time a buffer takes from a source to a sink, subject `src→sink` (core latency)",
        higher_is_worse=True,
    ),
    "pad.buffers": Metric("buffers/s", "Buffers pushed out of a source pad (core stats)"),
    "pad.bitrate": Metric("bit/s", "Bits pushed out of a source pad (core stats)"),
    "pad.interval": Metric(
        "ns", "Time between two buffers pushed out of a source pad (core stats)"
    ),
    "pad.lateness": Metric(
        "ns",
        "Clock time a buffer is pushed at minus the clock time it is due at, early when "
        "negative (buffer-lateness)",
        higher_is_worse=True,
    ),
    "pad.push": Metric(
        "ns",
        "Time a push out of a source pad takes, what downstream does on the thread included "
        "(pad-push-timings)",
        higher_is_worse=True,
    ),
    "queue.bytes": Metric("bytes", "Bytes a queue holds (queue-levels)", higher_is_worse=True),
    "queue.buffers": Metric(
        "buffers", "Buffers a queue holds (queue-levels)", higher_is_worse=True
    ),
    "queue.time": Metric("ns", "Time of data a queue holds (queue-levels)", higher_is_worse=True),
    "queue.fill": Metric(
        "ratio",
        "How full a queue is, 0 to 1: the highest of its levels over their non-zero limits "
        "(queue-levels)",
        higher_is_worse=True,
    ),
    "process.cpu": Metric(
        "%", "CPU time of the process over wall time; 100 is one core", higher_is_worse=True
    ),
    "thread.cpu": Metric(
        "%",
        "CPU time of a thread over wall time; 100 is one core. Threads are named by GStreamer "
        "after their pad (`q:src`); unnamed ones are `other`",
        higher_is_worse=True,
    ),
}
"""Every metric by name, with its unit: the live page and the report key on these names"""


def units_script() -> str:
    """`metrics.js`: the script that defines `METRIC_UNITS`, the unit of each metric, for
    `perf.js`, which the control page and the report both load after it."""
    units = {name: metric.unit for name, metric in METRICS.items()}
    return f"const METRIC_UNITS = {json.dumps(units, indent=2)};\n"
