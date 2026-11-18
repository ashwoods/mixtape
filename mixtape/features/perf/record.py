"""The recording of one run, for the `perf` feature (a player's run) and the pytest plugin (a
test's): its windows of what the tracers measured and the CPU of the process and its threads,
and the artifacts they make."""

import threading
import time
from pathlib import Path
from typing import Any

from ...perf.aggregate import RunStat, Window, rates, window
from ...perf.metrics import Sample
from ...perf.output import Artifacts
from ...perf.threads import IDLE, ThreadCpu
from .collect import Collector


def _subject(subject: str, keys: dict[str, str]) -> str:
    """`subject` (`element`, `element:pad` or `src→sink`) with element keys for names."""
    if "→" in subject:
        return "→".join(_subject(part, keys) for part in subject.split("→"))
    element, colon, pad = subject.partition(":")
    return keys.get(element, element) + colon + pad


class Recorder:
    """Record what the tracers measure from its creation on, in windows: each `window` closes
    the one collecting, from the end of the last. With `out`, a folder gets the artifacts (see
    `Artifacts`; `samples` says whether it gets every sample), written by `finish`.

    The rates (the CPU of the process and its threads, the buffers and bits per second of
    pads) are one value per window, so a window much shorter than `interval` would weigh as
    much as a whole one with a value of a few milliseconds: one shorter than half of it gets
    no rates, unless it is the first, and its time counts in the next window's, or in none
    when it is the last, the fraction of a second between the last poll and the stop.

    Any number of recorders can run at once, as the collector gives each what was measured
    since its own last window."""

    def __init__(
        self, out: Path | None = None, samples: bool = True, interval: float = 1.0
    ) -> None:
        self._collector = Collector.get()
        self._lock = threading.Lock()  # one window at a time: the poll's or the last
        self._artifacts = Artifacts(out, samples) if out is not None else None
        self._subscription = self._collector.start()
        self.start_ns = self.end_ns = self._collector.now()
        """Nanoseconds since GStreamer initialised: when it started, and where the window
        collecting starts, which is the end of the last"""
        self._shortest_ns = int(interval * 1e9 / 2)  # of a window with rates
        self._rates_ns = self.start_ns  # where the rates of the next window start
        self._rated = False  # whether a window has rates
        self._counts: dict[str, tuple[int, int]] = {}  # of pads since `_rates_ns`
        self._threads = ThreadCpu()
        self._threads.sample(self.start_ns)  # the baseline
        self._cpu = (time.process_time(), time.monotonic(), 0)  # at the rates' start
        self.measured: set[str] = set()
        """The metrics it has windows of"""

    @property
    def lost(self) -> int:
        """Times the debug log dropped lines before they were read since it started, so some
        tracer records are missing"""
        return self._subscription.lost

    def window(self, keys: dict[str, str] | None = None) -> Window:
        """Close the window collecting, with the subjects named by `keys` instead of the names
        the tracers report. It reads and writes files: not for a loop."""
        with self._lock:
            samples, counts = self._subscription.take()
            end = self._collector.now()
            for pad, (buffers, size) in counts.items():
                before = self._counts.get(pad, (0, 0))
                self._counts[pad] = (before[0] + buffers, before[1] + size)
            cpu = None
            if not self._rated or end - self._rates_ns >= self._shortest_ns:
                samples += rates(self._counts, self._rates_ns, end)
                samples += self._threads.sample(end)
                cpu = self._cpu_sample()  # after the threads: without what perf's own used
                self._rates_ns, self._counts, self._rated = end, {}, True
            if keys:
                samples = [s._replace(subject=_subject(s.subject, keys)) for s in samples]
            if cpu is not None:
                samples.append(cpu)
            # a thread idle in this window counts in the run's statistics, but is no point on
            # the chart
            shown = [s for s in samples if s.metric != "thread.cpu" or s.value >= IDLE]
            result = window(shown, self.end_ns, end)
            if self._artifacts is not None:
                self._artifacts.add(samples, result)
            self.measured.update(result.metrics)
            self.end_ns = end
            return result

    def stats(self) -> dict[str, dict[str, RunStat]]:
        """The statistics of the whole run so far, by metric and subject, from the windows
        closed: what `summary.json` has at the end. Needs `out`: the artifacts keep every value
        of the run (8 bytes each) for these, whether or not they write the samples."""
        with self._lock:
            assert self._artifacts is not None, "a recorder keeps the run only with `out`"
            return self._artifacts.totals.stats()

    def _cpu_sample(self) -> Sample | None:
        """`process.cpu` since the last window, but for what perf's own threads used
        (`thread.cpu` is `ThreadCpu`'s)."""
        cpu, wall, own = time.process_time(), time.monotonic(), self._threads.own_ns
        (last_cpu, last_wall, last_own), self._cpu = self._cpu, (cpu, wall, own)
        if wall <= last_wall:
            return None
        used = cpu - last_cpu - (own - last_own) / 1e9
        percent = max(used, 0.0) / (wall - last_wall) * 100
        return Sample(self._collector.now(), "process.cpu", "process", percent)

    def finish(self, about: dict[str, Any]) -> dict[str, Any] | None:
        """Stop recording and write the artifacts with `about` the run added to what it knows
        of it: their summary, or None without `out`."""
        with self._lock:
            lost = self.lost
            self._subscription.stop()
            if self._artifacts is None:
                return None
            return self._artifacts.finish(
                {
                    "tracers": self._collector.tracer_entries,
                    "start_ns": self.start_ns,
                    "end_ns": self.end_ns,
                    "lost": lost,
                    **about,
                }
            )

    def discard(self) -> None:
        """Stop recording and remove the folder, for a run with nothing to show."""
        with self._lock:
            self._subscription.stop()
            if self._artifacts is not None:
                self._artifacts.discard()
