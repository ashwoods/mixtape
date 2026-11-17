import argparse
import json
import logging
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mixtape import cli
from mixtape.cli import build_parser, main
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


def test_inspect_arrays_are_flags_only_when_their_items_are_a_choice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # GStreamer flags are arrays of a choice
    assert main(["inspect", "playbin name=pb"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.split()[:3] == ["flags", "rw", "flags"] for line in lines)
    # the tracers' names are a plain list; a process of its own, as the tracers need GStreamer
    # not to be initialised yet
    argv = [sys.executable, "-m", "mixtape", "inspect", "fakesrc ! fakesink"]
    done = subprocess.run(
        [*argv, "--feature", "tracers,names=latency"], capture_output=True, text=True, timeout=20
    )
    assert done.returncode == 0, done.stderr
    rows = [line.split()[:3] for line in done.stdout.splitlines()]
    assert ["tracers.active", "r-", "list"] in rows


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
    assert capsys.readouterr().err == (
        f"error: 127.0.0.1:{port} is in use (is another mixtape run --http running?): "
        "choose another port with --http PORT\n"
    )


def test_an_abbreviated_option_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", "--js", PIPELINE]) == 2
    assert "unrecognized arguments: --js" in capsys.readouterr().err
    assert main(["run", "--exp", PIPELINE]) == 2
    assert main(["perf", "compare", "--thr", "10", "a", "b"]) == 2


def test_verbose_counts_before_and_after_the_command() -> None:
    parser = build_parser()
    assert parser.parse_args(["-vv", "run", "x"]).verbose == 2
    args = parser.parse_args(["-v", "run", "-v", "x"])
    assert args.verbose + args.more_verbose == 2


def test_verbose_before_and_after_the_command_adds_up(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("mixtape")
    monkeypatch.setattr(logger, "level", logger.level)  # restored afterwards
    assert main(["-v", "run", "-v", "fakesrc num-buffers=1 ! fakesink"]) == 0
    assert logger.level == logging.DEBUG
    assert main(["-v", "run", "fakesrc num-buffers=1 ! fakesink"]) == 0
    assert logger.level == logging.INFO


def test_help_ends_with_examples_and_exit_statuses(capsys: pytest.CaptureFixture[str]) -> None:
    for command, statuses in [
        (["run"], "Exit status: 0 at EOS"),
        (["inspect"], "Exit status: 0 on success, 2 "),
        (["perf", "compare"], "Exit status: 0 when nothing got slower, 1 on a regression"),
    ]:
        assert main([*command, "--help"]) == 0
        out = " ".join(capsys.readouterr().out.split())
        assert statuses in out and "-v info, -vv debug" in out
    assert main(["run", "--help"]) == 0
    out = capsys.readouterr().out
    assert '  mixtape run "videotestsrc ! autovideosink"\n' in out
    assert "  mixtape run --http 8047 demo:Demo\n" in out
    assert "or gst:NAME for any GStreamer tracer" in " ".join(out.split())


def test_errors_start_in_lower_case(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "videotestsrc ! nosuchelement"]) == 2
    assert capsys.readouterr().err.startswith("error: invalid pipeline")
    assert main(["run", "--set", "src.nope=1", "videotestsrc name=src ! fakesink"]) == 2
    assert capsys.readouterr().err == (
        "error: unknown property 'src.nope': mixtape inspect TARGET lists them\n"
    )
    assert main(["run", "fakesink", "--feature", "nope"]) == 2
    assert capsys.readouterr().err.endswith("or gst:NAME for any GStreamer tracer\n")


def test_ctrl_c_outside_the_play_loop_exits_130_quietly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(args: argparse.Namespace) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_load_player", interrupted)
    assert main(["inspect", PIPELINE]) == 130
    assert capsys.readouterr().err == ""


NO_GI = (
    "import sys, types\n"
    "if sys.argv[1] == 'missing':\n"
    "    sys.modules['gi'] = None\n"  # `import gi` raises ImportError
    "else:\n"
    "    gi = types.ModuleType('gi')\n"  # GStreamer 1.0 is not installed
    "    def require_version(name, version):\n"
    "        raise ValueError('Namespace Gst not available')\n"
    "    gi.require_version = require_version\n"
    "    sys.modules['gi'] = gi\n"
    "from mixtape.cli import main\n"
    "sys.exit(main(sys.argv[2:]))\n"
)


def without_gi(how: str, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", NO_GI, how, *argv], capture_output=True, text=True, timeout=30
    )


@pytest.mark.parametrize("how", ["missing", "no-gst"])
def test_without_pygobject_help_and_version_work(how: str) -> None:
    done = without_gi(how, "--version")
    assert done.returncode == 0 and done.stdout.startswith("mixtape ")
    done = without_gi(how, "run", "--help")
    assert done.returncode == 0 and "Available: " in " ".join(done.stdout.split())


@pytest.mark.parametrize("how", ["missing", "no-gst"])
@pytest.mark.parametrize("command", ["run", "inspect"])
def test_without_pygobject_a_command_that_needs_it_says_so(how: str, command: str) -> None:
    done = without_gi(how, command, "fakesrc ! fakesink")
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == (
        "error: mixtape needs PyGObject and GStreamer 1.0 from the system Python: "
        "see https://shashi.to/mixtape/troubleshooting/\n"
    )


def test_without_pygobject_perf_compare_works(tmp_path: Path) -> None:
    done = without_gi("missing", "perf", "compare", str(tmp_path / "a"), str(tmp_path / "b"))
    assert done.returncode == 2
    assert done.stderr == f"error: {tmp_path / 'a'} does not exist\n"
