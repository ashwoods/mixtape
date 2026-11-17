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
    compared: bool = False
    """Whether `perf compare` looks at it, a higher value being a regression. Not rates and the
    interval between buffers, which say what the pipeline does, not how well; nor the push time
    and the lateness, which swing by tens of ms between two runs of the same short test (the
    push time has the sink's wait for the clock in it): a budget can bound them"""


METRICS: dict[str, Metric] = {
    "element.latency": Metric(
        "ns",
        "Time a buffer takes through an element, sink pad to source pad (core latency)",
        compared=True,
    ),
    "pipeline.latency": Metric(
        "ns",
        "Time a buffer takes from a source to a sink, subject `src→sink` (core latency)",
        compared=True,
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
    "queue.bytes": Metric("bytes", "Bytes a queue holds (queue-levels)", compared=True),
    "queue.buffers": Metric("buffers", "Buffers a queue holds (queue-levels)", compared=True),
    "queue.time": Metric("ns", "Time of data a queue holds (queue-levels)", compared=True),
    "queue.fill": Metric(
        "ratio",
        "How full a queue is, 0 to 1: the highest of its levels over their non-zero limits, "
        "1 when full, though a queue can hold one buffer past a limit (queue-levels)",
        compared=True,
    ),
    "process.cpu": Metric(
        "%", "CPU time of the process over wall time; 100 is one core", compared=True
    ),
    "thread.cpu": Metric(
        "%",
        "CPU time of a thread over wall time; 100 is one core. Threads are named by GStreamer "
        "after their pad (`q:src`); unnamed ones are `other`",
        compared=True,
    ),
}
"""Every metric by name, with its unit: the live page and the report key on these names"""


def units_script() -> str:
    """`metrics.js`: the script that defines `METRIC_UNITS`, the unit of each metric, for
    `perf.js`, which the control page and the report both load after it."""
    units = {name: metric.unit for name, metric in METRICS.items()}
    return f"const METRIC_UNITS = {json.dumps(units, indent=2)};\n"
