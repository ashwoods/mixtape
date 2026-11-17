import sys
import time
from typing import Any

import pytest

from mixtape.perf.threads import ThreadCpu

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
