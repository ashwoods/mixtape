"""The examples of docs/perf.md."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.doc_samples import ROOT, SCRUBBED
from tests.examples import run_example
from tests.flaky import in_time, timing

NEEDS = ("x264enc", "queue-levels")
MISSING = [
    name
    for name in NEEDS
    if subprocess.run(["gst-inspect-1.0", "--exists", name], capture_output=True).returncode
]
needs_recorder = pytest.mark.skipif(bool(MISSING), reason=f"needs the GStreamer {MISSING}")
# one after another with perf.md's samples, on one xdist worker: see tests/test_doc_samples.py
pytestmark = pytest.mark.xdist_group("perf")
# what the recordings of x264 measure, and how long they take, depends on the machine's load
flaky = pytest.mark.flaky(reason="x264's latency and run time depend on the machine's load")


def clean_env() -> dict[str, str]:
    """The environment without GStreamer settings a test of this process may have left in it."""
    return {k: v for k, v in os.environ.items() if k not in SCRUBBED}


def pytest_run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the suite of examples/recorder_suite in a pytest of its own, as the page does."""
    env = clean_env()
    env["PYTEST_ADDOPTS"] = "--timeout=60"  # not the 20 s per test of this project's ini
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "examples/recorder_suite", *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
        env=env,
    )


@needs_recorder
@flaky
@pytest.mark.timeout(120)
@pytest.mark.covers("perf_worst:worst")
def test_perf_worst_names_the_slow_encoder(tmp_path: Path) -> None:
    with in_time():
        recording = subprocess.run(
            [
                sys.executable,
                "-m",
                "mixtape",
                "run",
                "videotestsrc name=src is-live=true num-buffers=60 "
                "! video/x-raw,width=1280,height=720,framerate=30/1 ! queue name=q "
                "! x264enc name=enc speed-preset=slow threads=1 bitrate=4000 "
                f"! mp4mux name=mux ! filesink name=sink location={tmp_path}/rec.mp4",
                "--feature",
                f"perf,out={tmp_path}/slow",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
            env=clean_env(),
        )
    assert recording.returncode == 0, recording.stdout
    with in_time():
        out = run_example("perf_worst.py", str(tmp_path / "slow"))
    assert out.returncode == 0, out.stdout
    lines = [line.split() for line in out.stdout.splitlines()]
    assert [line[0] for line in lines] == [
        "metric",
        "element.latency",
        "pipeline.latency",
        "queue.fill",
        "process.cpu",
        "thread.cpu",
    ]
    assert lines[1][1] == "enc"
    assert lines[2][1] == "src→sink"


@pytest.mark.covers("perf_late:late")
def test_perf_after_gstreamer_is_initialised_is_refused() -> None:
    out = run_example("perf_late.py")
    assert out.returncode == 0, out.stdout
    assert out.stdout.startswith("The feature 'perf' needs the tracers 'latency', 'stats'")
    assert "before anything else initialises GStreamer" in out.stdout


# The slow encoder breaking the budget is perf.md's budget-fails sample (tests/samples/perf.py),
# which asserts the budget's message and that the 360p test, with no budget, passes. Running it
# here too took over 60 s on a busy machine (two x264 slow-preset recordings, one thread).
@needs_recorder
@flaky
@pytest.mark.timeout(90)
@pytest.mark.covers("test_recorder:recorder", "test_recorder:budgets")
def test_recorder_suite_passes_its_budgets(tmp_path: Path) -> None:
    with in_time():
        fast = pytest_run("--mixtape-perf", str(tmp_path / "fast"))
    index = json.loads((tmp_path / "fast" / "index.json").read_text())
    assert len(index["tests"]) == 2 and all(t["folder"] for t in index["tests"]), fast.stdout
    if fast.returncode != 0:  # over a budget is the machine's load; any other failure is not
        timing("perf budget:" not in fast.stdout, f"over its budgets\n{fast.stdout}")
    assert fast.returncode == 0, fast.stdout
    assert [t["outcome"] for t in index["tests"]] == ["passed", "passed"]
