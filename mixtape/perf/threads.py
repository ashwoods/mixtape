"""`thread.cpu`: the CPU time of each thread of the process, by the name GStreamer gave it
(after its task, the pad it pushes from: `q:src`), on Linux and macOS, with the standard
library only."""

import ctypes
import logging
import os
import sys
import threading
import time
from collections.abc import Callable

from mixtape.perf.metrics import Sample

logger = logging.getLogger(__name__)

OTHER = "other"
"""The subject of the threads without a name of their own"""

IDLE = 0.1
"""Percent of one core under which the threads of a name get no sample in a window, until
they have been over it once"""

OWN = "mixtape-perf"
"""What the names of perf's own threads start with: what they use is perf's, not the
pipeline's, so it is in no `thread.cpu` and taken out of `process.cpu`"""

_PROC_PIDTHREADINFO, _PROC_PIDLISTTHREADS = 5, 6


class _ThreadInfo(ctypes.Structure):
    """`struct proc_threadinfo` of libproc"""

    _fields_ = [
        ("user", ctypes.c_uint64),
        ("system", ctypes.c_uint64),
        ("cpu", ctypes.c_int32),
        ("policy", ctypes.c_int32),
        ("run_state", ctypes.c_int32),
        ("flags", ctypes.c_int32),
        ("sleep", ctypes.c_int32),
        ("curpri", ctypes.c_int32),
        ("priority", ctypes.c_int32),
        ("maxpriority", ctypes.c_int32),
        ("name", ctypes.c_char * 64),
    ]


def name_thread() -> None:
    """Give the calling thread its Python name in the operating system too, cut to the 15
    bytes Linux keeps, as Python itself does from 3.14 on, so that `ThreadCpu` sees it."""
    name = threading.current_thread().name.encode()[:15]
    try:
        if sys.platform == "linux":
            ctypes.CDLL(None).prctl(15, name)  # PR_SET_NAME
        elif sys.platform == "darwin":
            ctypes.CDLL(None).pthread_setname_np(name)
    except (OSError, AttributeError):  # a libc without it: the thread counts as the others
        logger.debug("cannot name the thread %r for the OS", name)


def parse_stat(stat: bytes, ticks: int) -> tuple[str, int]:
    """The name and the CPU time in ns of a thread from its `/proc/<pid>/task/<tid>/stat`, with
    `ticks` to the second. The kernel cuts the name to 15 bytes, in the middle of a character
    if it has to, so it is decoded with replacements."""
    # the name sits in parentheses and may hold spaces and parentheses itself: the first
    # opens it, the last closes it; the fields after are numbered from the third
    name = stat[stat.index(b"(") + 1 : stat.rindex(b")")].decode(errors="replace")
    fields = stat[stat.rindex(b")") + 2 :].split()
    used = int(fields[11]) + int(fields[12])  # utime, stime
    return name, used * 1_000_000_000 // ticks


class ThreadCpu:
    """Samples `thread.cpu`: the CPU time each thread used since the last `sample`, over the
    wall time since then, in percent of one core, summed by thread name. Perf's own threads
    (`OWN`) are left out, and what they used is summed in `own_ns`. Without a way to read
    threads on this OS, `sample` gives nothing."""

    def __init__(self) -> None:
        self._last: tuple[float, dict[int, int]] | None = None
        self._known: set[str] = set()  # the names that were over `IDLE` once
        self.own_ns = 0
        """CPU time perf's own threads used between the first `sample` and the last"""
        self._main = ""  # what Linux calls a thread nobody named: the process's own name
        self._libproc: ctypes.CDLL | None = None
        self._read: Callable[[], dict[int, tuple[str, int]]] | None = None
        if sys.platform == "linux":
            self._read = self._linux
            self._main = self._linux().get(os.getpid(), ("", 0))[0]
        elif sys.platform == "darwin":
            self._libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            self._read = self._macos
        else:
            logger.debug("thread.cpu is measured on Linux and macOS only, not on %s", sys.platform)

    def sample(self, t_ns: int) -> list[Sample]:
        """`thread.cpu` of each thread name since the last call, at `t_ns`: the first call
        only takes the baseline. A thread that came in between counts from zero, one that
        left is not counted, as what it used is not known. A name under `IDLE` is left out
        until it is over it once, and from then on has a sample in every window, so that the
        statistics of the run count the windows it was idle in."""
        if self._read is None:
            return []
        threads = self._read()
        wall = time.monotonic()
        last, self._last = self._last, (wall, {tid: ns for tid, (_, ns) in threads.items()})
        if last is None or wall <= last[0]:
            return []
        percents: dict[str, float] = {}
        for tid, (name, ns) in threads.items():
            before = last[1].get(tid, 0)
            used = ns - before if ns >= before else ns  # an id reused by a new thread
            if name.startswith(OWN):
                self.own_ns += used
                continue
            subject = name if name and name != self._main else OTHER
            percents[subject] = percents.get(subject, 0.0) + used / 1e9 / (wall - last[0]) * 100
        self._known.update(subject for subject, value in percents.items() if value >= IDLE)
        return [
            Sample(t_ns, "thread.cpu", subject, value)
            for subject, value in percents.items()
            if subject in self._known
        ]

    def _linux(self) -> dict[int, tuple[str, int]]:
        """Every thread by id: its name and CPU time in ns, from `/proc/self/task`"""
        ticks = os.sysconf("SC_CLK_TCK")
        threads: dict[int, tuple[str, int]] = {}
        for entry in os.listdir("/proc/self/task"):
            try:
                with open(f"/proc/self/task/{entry}/stat", "rb") as f:
                    stat = f.read()
            except OSError:  # the thread ended
                continue
            threads[int(entry)] = parse_stat(stat, ticks)
        return threads

    def _macos(self) -> dict[int, tuple[str, int]]:
        """Every thread by id: its name and CPU time in ns, from libproc"""
        assert self._libproc is not None
        pid, size = os.getpid(), 256
        while True:
            ids = (ctypes.c_uint64 * size)()
            n = (
                self._libproc.proc_pidinfo(pid, _PROC_PIDLISTTHREADS, 0, ids, ctypes.sizeof(ids))
                // 8
            )
            if n < size:
                break
            size *= 2
        threads: dict[int, tuple[str, int]] = {}
        for tid in ids[: max(n, 0)]:
            info = _ThreadInfo()
            got = self._libproc.proc_pidinfo(
                pid,
                _PROC_PIDTHREADINFO,
                ctypes.c_uint64(tid),
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if got > 0:  # else the thread ended
                threads[tid] = (info.name.decode(errors="replace"), info.user + info.system)
        return threads
