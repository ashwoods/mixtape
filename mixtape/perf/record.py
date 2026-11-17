"""The recording of one run, for the `perf` feature (a player's run) and the pytest plugin (a
test's): its windows of what the tracers measured and the CPU of the process, and the
artifacts they make."""

import threading
import time
from pathlib import Path
from typing import Any

from .aggregate import Window, rates, window
from .collect import Collector
from .metrics import Sample
from .output import Artifacts


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

    Any number of recorders can run at once, as the collector gives each what was measured
    since its own last window."""

    def __init__(self, out: Path | None = None, samples: bool = True) -> None:
        self._collector = Collector.get()
        self._lock = threading.Lock()  # one window at a time: the poll's or the last
        self._artifacts = Artifacts(out, samples) if out is not None else None
        self._subscription = self._collector.start()
        self.start_ns = self.end_ns = self._collector.now()
        """Nanoseconds since GStreamer initialised: when it started, and where the window
        collecting starts, which is the end of the last"""
        self._cpu = (time.process_time(), time.monotonic())  # at the window's start
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
            cpu = self._cpu_sample()
            samples, counts = self._subscription.take()
            end = self._collector.now()
            samples += rates(counts, self.end_ns, end)
            if keys:
                samples = [s._replace(subject=_subject(s.subject, keys)) for s in samples]
            if cpu is not None:
                samples.append(cpu)
            result = window(samples, self.end_ns, end)
            if self._artifacts is not None:
                self._artifacts.add(samples, result)
            self.measured.update(result.metrics)
            self.end_ns = end
            return result

    def _cpu_sample(self) -> Sample | None:
        """`process.cpu` since the last window."""
        cpu, wall = time.process_time(), time.monotonic()
        (last_cpu, last_wall), self._cpu = self._cpu, (cpu, wall)
        if wall <= last_wall:
            return None
        percent = (cpu - last_cpu) / (wall - last_wall) * 100
        return Sample(self._collector.now(), "process.cpu", "process", percent)

    def finish(self, about: dict[str, Any]) -> dict[str, Any] | None:
        """Stop recording and write the artifacts with `about` the run added to what it knows
        of it: their summary, or None without `out`."""
        with self._lock:
            self._subscription.stop()
            if self._artifacts is None:
                return None
            return self._artifacts.finish(
                {
                    "tracers": self._collector.tracer_entries,
                    "start_ns": self.start_ns,
                    "end_ns": self.end_ns,
                    **about,
                }
            )

    def discard(self) -> None:
        """Stop recording and remove the folder, for a run with nothing to show."""
        with self._lock:
            self._subscription.stop()
            if self._artifacts is not None:
                self._artifacts.discard()
