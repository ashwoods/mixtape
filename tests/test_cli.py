import queue
import signal
import subprocess
import sys
import threading
import time

import pytest

from mixtape.cli import main


def test_run_until_eos() -> None:
    assert main(["run", "videotestsrc", "num-buffers=10", "!", "fakesink"]) == 0


def test_run_pipeline_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "filesrc ! fakesink"]) == 1
    assert "error: filesrc0:" in capsys.readouterr().err


def test_run_bad_description(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "videotestsrc ! nosuchelement"]) == 2
    assert "error:" in capsys.readouterr().err


def test_no_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("mixtape ")


def test_sigint_stops_with_eos() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "mixtape", "-vv", "run", "videotestsrc is-live=true ! fakesink"],
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr = process.stderr
    assert stderr is not None
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        for line in stderr:
            lines.put(line)

    threading.Thread(target=read, daemon=True).start()
    seen: list[str] = []
    try:
        deadline = time.monotonic() + 10
        while not any("to playing" in line for line in seen):
            seen.append(lines.get(timeout=max(deadline - time.monotonic(), 0.01)))
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()
        process.wait()
    while not lines.empty():
        seen.append(lines.get_nowait())
    assert any("Interrupt: stopping (EOS)" in line for line in seen), seen
