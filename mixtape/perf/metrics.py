"""What `perf` measures: the samples and the fixed table of metrics they belong to."""

import json
from typing import NamedTuple


class Sample(NamedTuple):
    """One measurement: at `t_ns` (nanoseconds since GStreamer initialised, the clock of its
    tracers), `metric` of `subject` (an element, `element:pad`, `src→sink` or `process`)
    had `value`, in the unit of the metric."""

    t_ns: int
    metric: str
    subject: str
    value: float


class Metric(NamedTuple):
    unit: str
    description: str


METRICS: dict[str, Metric] = {
    "element.latency": Metric(
        "ns", "Time a buffer takes through an element, sink pad to source pad (core latency)"
    ),
    "pipeline.latency": Metric(
        "ns", "Time a buffer takes from a source to a sink, subject `src→sink` (core latency)"
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
    ),
    "pad.push": Metric(
        "ns",
        "Time a push out of a source pad takes, what downstream does on the thread included "
        "(pad-push-timings)",
    ),
    "queue.bytes": Metric("bytes", "Bytes a queue holds (queue-levels)"),
    "queue.buffers": Metric("buffers", "Buffers a queue holds (queue-levels)"),
    "queue.time": Metric("ns", "Time of data a queue holds (queue-levels)"),
    "queue.fill": Metric(
        "ratio",
        "How full a queue is, 0 to 1: the highest of its levels over their non-zero limits "
        "(queue-levels)",
    ),
    "process.cpu": Metric("%", "CPU time of the process over wall time; 100 is one core"),
}
"""Every metric by name, with its unit: the live page and the report key on these names"""


def units_script() -> str:
    """`metrics.js`: the script that defines `METRIC_UNITS`, the unit of each metric, for
    `perf.js`, which the control page and the report both load after it."""
    units = {name: metric.unit for name, metric in METRICS.items()}
    return f"const METRIC_UNITS = {json.dumps(units, indent=2)};\n"
