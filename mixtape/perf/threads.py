"""`thread.cpu`: the CPU time of each thread of the process, by the name GStreamer gave it
(after its task, the pad it pushes from: `q:src`), on Linux and macOS, with the standard
library only."""

import ctypes
import logging
import os
import sys
import time
from collections.abc import Callable

from mixtape.perf.metrics import Sample

logger = logging.getLogger(__name__)

OTHER = "other"
"""The subject of the threads without a name of their own"""

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


class ThreadCpu:
    """Samples `thread.cpu`: the CPU time each thread used since the last `sample`, over the
    wall time since then, in percent of one core, summed by thread name. Without a way to
    read threads on this OS, `sample` gives nothing."""

    def __init__(self) -> None:
        self._last: tuple[float, dict[int, int]] | None = None
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
        left is not counted, as what it used is not known."""
        if self._read is None:
            return []
        threads = self._read()
        wall = time.monotonic()
        last, self._last = self._last, (wall, {tid: ns for tid, (_, ns) in threads.items()})
        if last is None or wall <= last[0]:
            return []
        percents: dict[str, float] = {}
        for tid, (name, ns) in threads.items():
            subject = name if name and name != self._main else OTHER
            used = ns - last[1].get(tid, 0)
            percents[subject] = percents.get(subject, 0.0) + used / 1e9 / (wall - last[0]) * 100
        return [Sample(t_ns, "thread.cpu", subject, value) for subject, value in percents.items()]

    def _linux(self) -> dict[int, tuple[str, int]]:
        """Every thread by id: its name and CPU time in ns, from `/proc/self/task`"""
        ticks = os.sysconf("SC_CLK_TCK")
        threads: dict[int, tuple[str, int]] = {}
        for entry in os.listdir("/proc/self/task"):
            try:
                with open(f"/proc/self/task/{entry}/stat") as f:
                    stat = f.read()
            except OSError:  # the thread ended
                continue
            # the name sits in parentheses and may hold spaces and parentheses itself: the first
            # opens it, the last closes it; the fields after are numbered from the third
            name = stat[stat.index("(") + 1 : stat.rindex(")")]
            fields = stat[stat.rindex(")") + 2 :].split()
            used = int(fields[11]) + int(fields[12])  # utime, stime
            threads[int(entry)] = (name, used * 1_000_000_000 // ticks)
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
