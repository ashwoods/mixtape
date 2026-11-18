import sys
import threading
import time
from typing import Any

import pytest

from mixtape.perf.threads import ThreadCpu, name_thread, parse_stat
from tests.flaky import timing

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="threads are read on Linux and macOS"
)

PIPELINE = (
    # short names: Linux cuts a thread's name to 15 bytes (`videotestsrc36:`, its pad gone).
    # Not live, so it keeps both threads busy: Linux counts a thread's CPU in ticks of 10 ms,
    # which a live stream using 30 ms a second would get wrong by far more than the 25%.
    "videotestsrc name=s ! video/x-raw,width=320,height=240 ! videoconvert "
    "! queue name=q ! fakesink"
)


@pytest.mark.flaky(reason="on a busy machine the threads get less of the CPU than they ask")
def test_thread_cpu_names_the_streaming_threads_and_adds_up_to_the_process(Gst: Any) -> None:
    pipeline = Gst.parse_launch(PIPELINE)
    sampler = ThreadCpu()
    pipeline.set_state(Gst.State.PLAYING)
    try:
        time.sleep(0.3)  # let the threads start
        assert sampler.sample(0) == []  # the baseline
        wall, cpu = time.monotonic(), time.process_time()
        time.sleep(1)
        samples = sampler.sample(1)
        wall, cpu = time.monotonic() - wall, time.process_time() - cpu
    finally:
        pipeline.set_state(Gst.State.NULL)
    assert {s.metric for s in samples} == {"thread.cpu"}
    assert any(":src" in s.subject for s in samples), [s.subject for s in samples]
    threads, process = sum(s.value for s in samples), cpu / wall * 100
    timing(
        threads == pytest.approx(process, rel=0.25),
        f"threads {threads:.0f}%, process {process:.0f}%",
    )


def test_a_name_cut_in_the_middle_of_a_character_is_decoded_with_replacements() -> None:
    # the kernel cuts comm to 15 bytes: "é" is two bytes, and this ends with half of one
    name = "streaming-thr-é".encode()[:15]
    assert name.endswith(b"\xc3")
    fields = " ".join(["S", "1", "1", "1", "0", "-1", "0", "0", "0", "0", "0", "7", "5"])
    stat = b"42 (" + name + b") " + fields.encode() + b" 0 0\n"
    assert parse_stat(stat, 100) == ("streaming-thr-\ufffd", 120_000_000)
    assert parse_stat(b"1 (a) b (c)) " + fields.encode() + b"\n", 100)[0] == "a) b (c)"


def test_a_thread_that_goes_idle_counts_as_idle_and_perfs_own_threads_not_at_all() -> None:
    sampler = ThreadCpu()
    reads = iter(
        [
            {1: ("q:src", 0), 2: ("mixtape-perf-log", 0), 3: ("idle", 0)},
            {1: ("q:src", 10**12), 2: ("mixtape-perf-log", 10**12), 3: ("idle", 0)},
            {1: ("q:src", 10**12), 2: ("mixtape-perf-log", 3 * 10**12), 3: ("idle", 0)},
        ]
    )
    sampler._read = lambda: next(reads)  # pyright: ignore[reportPrivateUsage]
    assert sampler.sample(0) == []
    (busy,) = sampler.sample(1)
    (idle,) = sampler.sample(2)  # busy once: a sample in every window from then on
    assert (busy.subject, idle.subject, idle.value) == ("q:src", "q:src", 0)
    assert busy.value > 0
    assert sampler.own_ns == 3 * 10**12


def test_perfs_own_threads_have_their_name_for_the_os() -> None:
    stop = threading.Event()

    def spin() -> None:
        name_thread()
        while not stop.is_set():
            pass

    sampler = ThreadCpu()
    sampler.sample(0)
    thread = threading.Thread(target=spin, name="mixtape-perf-test")
    thread.start()
    try:
        time.sleep(0.3)
        samples = sampler.sample(1)
    finally:
        stop.set()
        thread.join()
    assert sampler.own_ns > 10_000_000, samples  # the spin, not in a subject of its own
    assert all(not s.subject.startswith("mixtape") for s in samples)


def test_a_thread_id_that_a_new_thread_reuses_counts_from_zero() -> None:
    sampler = ThreadCpu()
    reads = iter([{7: ("q:src", 5_000_000_000)}, {7: ("q:src", 100_000_000)}])
    sampler._read = lambda: next(reads)  # pyright: ignore[reportPrivateUsage]
    assert sampler.sample(0) == []
    (sample,) = sampler.sample(1)
    assert sample.subject == "q:src" and sample.value >= 0
