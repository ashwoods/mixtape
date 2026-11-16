# type: ignore
"""`mixtape run` against the real `faulty` and `leaky` elements of gst-c-crash
(https://github.com/ashwoods/gst-c-crash). Skipped unless they are found, as on CI: build the
plugin and put it on `GST_PLUGIN_PATH` to run these."""

import os
import signal
import subprocess
import sys
import time

import pytest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape.player import ensure_init

ensure_init()
pytestmark = pytest.mark.skipif(
    not (Gst.ElementFactory.find("faulty") and Gst.ElementFactory.find("leaky")),
    reason="gst-c-crash is not on GST_PLUGIN_PATH",
)

SRC = "videotestsrc num-buffers=110 ! video/x-raw,width=64,height=48"


def run(*args: str, interrupt_after: float | None = None) -> tuple[int, str]:
    process = subprocess.Popen(
        [sys.executable, "-m", "mixtape", "run", *args], stderr=subprocess.PIPE, text=True
    )
    try:
        if interrupt_after is not None:
            time.sleep(interrupt_after)
            process.send_signal(signal.SIGINT)
        _, err = process.communicate(timeout=20)
    finally:
        process.kill()
    return process.returncode, err


def test_an_error_while_playing():
    code, err = run(f"{SRC} ! faulty name=f method=gst num-buffers=20 ! fakesink")
    assert code == 1
    assert "error: f: Cannot do this anymore." in err


def test_a_failed_start():
    code, err = run(f"{SRC} ! faulty name=f method=start ! fakesink")
    assert code == 1
    assert "error: Could not set the pipeline to playing" in err


def test_a_failed_stop():
    code, err = run(f"{SRC} ! faulty name=f method=stop ! fakesink")
    assert code == 1
    assert "error: Could not set the pipeline to null: f failed" in err


def test_a_hang_in_the_preroll_ends_on_one_interrupt():
    started = time.monotonic()
    hangs = f"{SRC} ! faulty name=f method=hang num-buffers=0 ! fakesink"
    code, err = run(hangs, interrupt_after=1)
    assert code == 1
    assert "error: Could not set the pipeline to null within 5 s: f still changing" in err
    assert time.monotonic() - started < 10


def test_leaks_names_leakys_buffers():
    env = {key: value for key, value in os.environ.items() if key != "GST_TRACERS"}
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "mixtape",
            "run",
            "--feature",
            "leaks",
            f"{SRC} ! leaky method=ref ! fakesink",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert done.returncode == 0
    assert "Still alive after the run: " in done.stderr
    assert "10 GstBuffer" in done.stderr
