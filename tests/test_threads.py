import sys
import time
from typing import Any

import pytest

from mixtape.perf.threads import ThreadCpu, parse_stat

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="threads are read on Linux and macOS"
)

PIPELINE = (
    "videotestsrc is-live=true ! video/x-raw,width=640,height=480 ! videoconvert "
    "! queue name=q ! fakesink"
)


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
    assert sum(s.value for s in samples) == pytest.approx(cpu / wall * 100, rel=0.25)


def test_a_name_cut_in_the_middle_of_a_character_is_decoded_with_replacements() -> None:
    # the kernel cuts comm to 15 bytes: "é" is two bytes, and this ends with half of one
    name = "streaming-thr-é".encode()[:15]
    assert name.endswith(b"\xc3")
    fields = " ".join(["S", "1", "1", "1", "0", "-1", "0", "0", "0", "0", "0", "7", "5"])
    stat = b"42 (" + name + b") " + fields.encode() + b" 0 0\n"
    assert parse_stat(stat, 100) == ("streaming-thr-\ufffd", 120_000_000)
    assert parse_stat(b"1 (a) b (c)) " + fields.encode() + b"\n", 100)[0] == "a) b (c)"


def test_a_thread_id_that_a_new_thread_reuses_counts_from_zero() -> None:
    sampler = ThreadCpu()
    reads = iter([{7: ("q:src", 5_000_000_000)}, {7: ("q:src", 100_000_000)}])
    sampler._read = lambda: next(reads)  # pyright: ignore[reportPrivateUsage]
    assert sampler.sample(0) == []
    (sample,) = sampler.sample(1)
    assert sample.subject == "q:src" and sample.value >= 0
