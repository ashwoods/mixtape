"""A live 720p recorder as a test suite, measured and budgeted by mixtape's pytest plugin.

pytest examples/recorder_suite --mixtape-perf /tmp/perf-out

RECORDER_ENCODER sets the options of x264enc, to break the budgets on purpose.
"""

# --8<-- [start:recorder]
import os
from pathlib import Path

import gi
import pytest

gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)  # the plugin has done it already when it measures

ENCODER = os.environ.get("RECORDER_ENCODER", "speed-preset=ultrafast tune=zerolatency")


def record(path: Path, width: int, height: int) -> None:
    """Record two seconds of a live test source to an MP4, until its end of stream."""
    pipeline = Gst.parse_launch(
        f"videotestsrc name=src is-live=true num-buffers=60 "
        f"! video/x-raw,width={width},height={height},framerate=30/1 "
        f"! queue name=q ! x264enc name=enc threads=1 bitrate=4000 {ENCODER} "
        f"! mp4mux name=mux ! filesink name=sink location={path}"
    )
    pipeline.set_state(Gst.State.PLAYING)
    ended = Gst.MessageType.EOS | Gst.MessageType.ERROR
    message = pipeline.get_bus().timed_pop_filtered(60 * Gst.SECOND, ended)
    pipeline.set_state(Gst.State.NULL)
    assert message is not None and message.type == Gst.MessageType.EOS


# --8<-- [end:recorder]


# --8<-- [start:budgets]
@pytest.mark.perf(pipeline_latency_p95="500ms", process_cpu_mean="150%")
def test_records_720p(tmp_path: Path) -> None:
    record(tmp_path / "rec.mp4", 1280, 720)
    assert (tmp_path / "rec.mp4").stat().st_size > 0


def test_records_360p(tmp_path: Path) -> None:  # measured, but with no budget
    record(tmp_path / "rec.mp4", 640, 360)
    assert (tmp_path / "rec.mp4").stat().st_size > 0


# --8<-- [end:budgets]
