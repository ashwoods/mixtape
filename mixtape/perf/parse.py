"""Parsers of what GStreamer's tracers write: the `GST_TRACER` debug log of the core `latency`
and `stats` tracers, and the CSV logs of the gst-plugins-rs tracers.

Both run off the streaming threads and off the loop: they read files GStreamer wrote."""

import re
from collections.abc import Callable, Iterable

from .metrics import Sample

RECORD = "GST_TRACER :0:: "
"""What precedes a record in a log line, after the category name"""

# A record prints a string with `%s`, as it is: a name with a comma or a quote (`a,b`) is not
# quoted, so a string field ends at the next field the record declares, not at a comma
_STR = r"(.*?)"
_U64 = r"\(guint64\)(\d+)"
# the fields of a record come in the order its GstTracerRecord declares them
_ELEMENT_LATENCY = re.compile(
    rf", element=\(string\){_STR}, src=\(string\).*?, time={_U64}, ts={_U64}"
)
_LATENCY = re.compile(
    rf", src-element=\(string\){_STR}, src=\(string\).*?"
    rf", sink-element=\(string\){_STR}, sink=\(string\).*?, time={_U64}, ts={_U64}"
)
_NEW_ELEMENT = re.compile(
    rf"ts={_U64}, ix=\(uint\)(\d+), parent-ix=\(uint\)\d+, name=\(string\){_STR}, type="
)
_NEW_PAD = re.compile(rf"ix=\(uint\)(\d+), parent-ix=\(uint\)(\d+), name=\(string\){_STR}, type=")
_BUFFER = re.compile(
    rf"ts={_U64}, pad-ix=\(uint\)(\d+), element-ix=\(uint\)(\d+), .*?buffer-size=\(uint\)(\d+)"
)


class TracerLog:
    """Parse `GST_TRACER` debug log lines into samples: `element.latency` and
    `pipeline.latency` from `latency`, `pad.interval` from the `buffer` records of `stats`,
    whose buffers and bytes per source pad it counts for `pad.buffers` and `pad.bitrate`.

    It keeps the names `stats` gives its indices (`new-element`, `new-pad`) for good: they
    come once per process. Other records (events, queries, messages) are ignored.
    Not thread safe: one thread feeds it."""

    def __init__(self) -> None:
        self.collecting = True
        """Whether it keeps samples and counts; it reads the names either way"""
        self.elements: dict[int, str] = {}
        self.created: dict[str, int] = {}  # element name: ts of its last `new-element`
        self.pads: dict[int, str] = {}  # pad index: its name
        self.samples: list[Sample] = []
        self.counts: dict[str, list[int]] = {}  # `element:pad`: [buffers, bytes]
        self._last: dict[str, int] = {}  # `element:pad`: ts of its last buffer

    def reset(self) -> None:
        """Forget the samples, counts and last buffers, not the names."""
        self.samples = []
        self.counts = {}
        self._last = {}

    def take(self) -> tuple[list[Sample], dict[str, tuple[int, int]]]:
        """The samples since the last call, and the buffers and bytes per `element:pad`;
        every pad seen since `reset` is there, with zeros when it pushed none."""
        samples, self.samples = self.samples, []
        counts = {subject: (c[0], c[1]) for subject, c in self.counts.items()}
        for c in self.counts.values():
            c[0] = c[1] = 0
        return samples, counts

    def line(self, line: str) -> bool:
        """Parse a line of the log. Whether it is a line of the `GST_TRACER` category, which
        a debug log meant for people would not show: the rest is the user's `GST_DEBUG`."""
        # time, pid, thread, level, category, then `file:line:function:object message`
        fields = line.split(None, 5)
        if len(fields) < 6 or fields[4] != "GST_TRACER":
            return False
        if fields[5].startswith(":0:: "):
            self.record(fields[5][5:])
        return True

    def record(self, payload: str) -> None:
        """Parse the payload of a record: `name, field=(type)value, …;`."""
        name = payload[: payload.find(",")]
        if name == "buffer":
            if self.collecting and (m := _BUFFER.search(payload)):
                self._buffer(int(m[1]), int(m[2]), int(m[3]), int(m[4]))
        elif name == "element-latency":
            if self.collecting and (m := _ELEMENT_LATENCY.search(payload)):
                self.samples.append(Sample(int(m[3]), "element.latency", m[1], int(m[2])))
        elif name == "latency":
            if self.collecting and (m := _LATENCY.search(payload)):
                subject = f"{m[1]}→{m[2]}"
                self.samples.append(Sample(int(m[4]), "pipeline.latency", subject, int(m[3])))
        elif name == "new-element":
            if m := _NEW_ELEMENT.search(payload):
                name = self.elements[int(m[2])] = m[3]
                self.created[name] = int(m[1])
        elif name == "new-pad":
            if m := _NEW_PAD.search(payload):
                self.pads[int(m[1])] = m[3]

    def _buffer(self, ts: int, pad: int, element: int, size: int) -> None:
        subject = f"{self.elements.get(element, element)}:{self.pads.get(pad, pad)}"
        if (counts := self.counts.get(subject)) is None:
            counts = self.counts[subject] = [0, 0]
        counts[0] += 1
        counts[1] += size
        if (last := self._last.get(subject)) is not None:
            self.samples.append(Sample(ts, "pad.interval", subject, ts - last))
        self._last[subject] = ts


def _columns(line: str, numbers: int) -> tuple[int, str, list[int]] | None:
    """`ts,name,pointer,number…` as (ts, name, numbers); a name may have commas, as the CSV
    is not quoted."""
    parts = line.rstrip("\n").split(",")
    if len(parts) < numbers + 3:
        return None
    try:
        return int(parts[0]), ",".join(parts[1 : -numbers - 1]), [int(p) for p in parts[-numbers:]]
    except ValueError:
        return None


def queue_levels(lines: Iterable[str]) -> list[Sample]:
    """`queue.*` from the `queue-levels` CSV: `ts, queue (or multiqueue:index), pointer,
    current bytes, time and buffers, maximum bytes, time and buffers`.

    `queue.fill` stops at 1: a queue waits only while a level is at or over its limit, then
    takes the next buffer whole, so its bytes or time can pass the limit by one buffer
    (`gst_queue_is_filled` in gstqueue.c). The levels themselves are kept as they are."""
    samples: list[Sample] = []
    for line in lines:
        if (row := _columns(line, 6)) is None:
            continue
        ts, queue, (cur_bytes, cur_time, cur_buffers, max_bytes, max_time, max_buffers) = row
        samples += (
            Sample(ts, "queue.bytes", queue, cur_bytes),
            Sample(ts, "queue.buffers", queue, cur_buffers),
            Sample(ts, "queue.time", queue, cur_time),
        )
        limits = ((cur_bytes, max_bytes), (cur_time, max_time), (cur_buffers, max_buffers))
        if fills := [cur / limit for cur, limit in limits if limit]:
            samples.append(Sample(ts, "queue.fill", queue, min(1.0, max(fills))))
    return samples


def buffer_lateness(lines: Iterable[str]) -> list[Sample]:
    """`pad.lateness` from the `buffer-lateness` CSV: `ts, element:pad, pointer, clock time
    the buffer is due at, clock time it is pushed at, lateness (the second minus the first),
    the minimum latency a live pipeline reported at the pad (0 when not live)`."""
    return [
        Sample(row[0], "pad.lateness", row[1], row[2][2])
        for line in lines
        if (row := _columns(line, 4)) is not None
    ]


def pad_push_timings(lines: Iterable[str]) -> list[Sample]:
    """`pad.push` from the `pad-push-timings` CSV: `ts (the push started), element:pad,
    pointer, push duration`."""
    return [
        Sample(row[0], "pad.push", row[1], row[2][0])
        for line in lines
        if (row := _columns(line, 1)) is not None
    ]


RUST: dict[str, Callable[[Iterable[str]], list[Sample]]] = {
    "queue-levels": queue_levels,
    "buffer-lateness": buffer_lateness,
    "pad-push-timings": pad_push_timings,
}
"""The parser of the `write-log` CSV of each gst-plugins-rs tracer, by name"""
