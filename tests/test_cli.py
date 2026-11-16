import json
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from mixtape.cli import main
from tests.players import Feat, register


def test_run_until_eos() -> None:
    assert main(["run", "videotestsrc", "num-buffers=10", "!", "fakesink"]) == 0


def test_run_pipeline_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "filesrc ! fakesink"]) == 1
    assert "error: filesrc0:" in capsys.readouterr().err


def test_run_bad_description(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "videotestsrc ! nosuchelement"]) == 2
    assert "error:" in capsys.readouterr().err


PIPELINE = "videotestsrc name=src pattern=snow num-buffers=5 ! fakesink"


def test_inspect_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", PIPELINE]) == 0
    out = capsys.readouterr().out
    assert "src (videotestsrc) — Video test source" in out
    assert any("pattern" in line and "rw" in line and "= snow" in line for line in out.splitlines())


def test_inspect_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", "--json", "videotestsrc", "name=src", "!", "fakesink"]) == 0
    spec = json.loads(capsys.readouterr().out)
    assert spec["elements"]["src"]["properties"]["pattern"]["schema"]["enum"][0] == "smpte"


def test_inspect_expose_all(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", PIPELINE]) == 0
    assert "(fakesink)" not in capsys.readouterr().out
    assert main(["inspect", PIPELINE, "--expose-all"]) == 0
    assert re.search(r"^fakesink\d+ \(fakesink\)", capsys.readouterr().out, re.MULTILINE)


def test_inspect_player_class(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", "tests.players:Demo"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Demo\n")
    assert "→ src.pattern" in out
    assert "shout(word: string, times: integer = 2)" in out


@pytest.mark.parametrize("position", ["before", "after"])
def test_run_set(position: str) -> None:
    description = ["videotestsrc", "name=src", "num-buffers=5", "!", "fakesink"]
    argv = ["run", "--set", "src.pattern=snow", *description]
    if position == "after":
        argv = ["run", *description, "--set", "src.pattern=snow", "--set", "src.is-live=false"]
    assert main(argv) == 0


@pytest.mark.parametrize(
    "assignment", ["src.kx=x", "src.num-buffers=-9", "src.nope=1", "sink.sync=false"]
)
def test_run_set_rejected(assignment: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--set", assignment, "videotestsrc name=src ! fakesink"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and len(err.splitlines()) == 1


def test_run_set_needs_a_value(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--set", "src.pattern", "videotestsrc ! fakesink"]) == 2
    assert "PATH=VALUE" in capsys.readouterr().err


def test_run_player_class() -> None:
    assert main(["run", "tests.players:Demo"]) == 0


def test_inspect_feature(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    register(monkeypatch, Feat)
    assert main(["inspect", PIPELINE, "--feature", "feat,step=0.5"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == ["Player", "Features: feat"]
    assert any(line.lstrip().startswith("feat.bump(times: integer = 1)") for line in lines)
    assert any("feat.level" in line and "r-" in line and "= 0" in line for line in lines)
    assert main(["inspect", "--json", PIPELINE, "--feature", "feat"]) == 0
    assert "feat.level" in json.loads(capsys.readouterr().out)["properties"]
    assert main(["inspect", "tests.players:Demo", "--feature", "feat"]) == 0
    assert "Features: feat" in capsys.readouterr().out
    assert main(["run", PIPELINE, "--feature", "feat"]) == 0


def test_help_lists_the_features(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    register(monkeypatch, Feat)
    assert main(["inspect", "--help"]) == 0
    assert "Available: feat" in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("command", ["run", "inspect"])
@pytest.mark.parametrize(
    "feature", ["nofeat", "feat,step=x", "feat,step"], ids=["unknown", "bad-option", "no-value"]
)
def test_a_bad_feature_is_a_usage_error(
    command: str,
    feature: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    register(monkeypatch, Feat)
    assert main([command, PIPELINE, "--feature", feature]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and len(err.splitlines()) == 1
    if feature == "nofeat":
        assert "available: feat" in err


@pytest.mark.parametrize(
    "target",
    [
        "nosuchmodule:Demo",
        "tests.players:Nope",
        "tests.players:NOT_A_PLAYER",
        "tests.players:NoDescription",
    ],
)
def test_bad_target(target: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", target]) == 2
    assert capsys.readouterr().err.startswith("error: ")


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


@pytest.mark.parametrize("address", ["", "http", "1:99999", "host:", "-1"])
def test_http_address_is_validated(address: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--http", address, "fakesink"]) == 2
    assert "[HOST:]PORT" in capsys.readouterr().err


def test_http_needs_the_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "mixtape.interfaces.http", None)  # as if not installed
    assert main(["run", "--http", "8080", "fakesink"]) == 2
    assert capsys.readouterr().err == (
        "error: --http needs the http extra: pip install 'mixtape[http]'\n"
    )


def test_http_port_in_use(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.create_server(("127.0.0.1", 0)) as taken:
        port = taken.getsockname()[1]
        assert main(["run", "--http", f"127.0.0.1:{port}", "fakesink"]) == 1
    assert capsys.readouterr().err.startswith("error: ")
