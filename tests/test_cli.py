import json
import logging
import os
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
    spec = json.loads(capsys.readouterr().out)
    assert spec["features"] == ["feat"] and "feat.level" in spec["properties"]
    assert main(["inspect", "--json", PIPELINE]) == 0
    assert json.loads(capsys.readouterr().out)["features"] == []
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
    err = capsys.readouterr().err
    assert err.startswith("Usage: mixtape [OPTIONS] COMMAND [ARGS]...") and "Commands:" in err


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
    err = capsys.readouterr().err
    assert err.startswith("error: no such option: --js") and "\nUsage: mixtape inspect " in err
    assert main(["run", "--exp", PIPELINE]) == 2
    assert main(["perf", "compare", "--thr", "10", "a", "b"]) == 2


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["run", PIPELINE, "--http"], "mixtape run"),
        (["perf", "compare", "a", "--threshold"], "mixtape perf compare"),
    ],
)
def test_an_option_without_its_value_points_at_its_command_help(
    argv: list[str], command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # refused before the command has a context: reported by the review of round 4
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: option '--") and err.endswith(
        f"Try '{command} --help' for help.\n"
    )


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
        (["inspect"], "Exit status: 0 on success, 1 without PyGObject, 2 "),
        (["perf", "compare"], "Exit status: 0 when nothing got slower, 1 on a regression"),
    ]:
        assert main([*command, "--help"]) == 0
        out = " ".join(capsys.readouterr().out.split())
        assert statuses in out and "-v info, -vv debug" in out
    assert main(["run", "--help"]) == 0
    out = capsys.readouterr().out
    assert '  mixtape run "videotestsrc ! autovideosink"\n' in out
    assert "  mixtape run --http 8047 examples/demo.py:Demo\n" in out
    assert "or gst:NAME for any GStreamer tracer" in " ".join(out.split())


def test_errors_start_in_lower_case(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "videotestsrc ! nosuchelement"]) == 2
    assert capsys.readouterr().err.startswith("error: invalid pipeline")
    assert main(["run", "--set", "src.nope=1", "videotestsrc name=src ! fakesink"]) == 2
    assert capsys.readouterr().err == (
        "error: unknown property 'src.nope': mixtape inspect TARGET lists them\n"
    )
    assert main(["run", "--set", "src.patern=1", "videotestsrc name=src ! fakesink"]) == 2
    assert capsys.readouterr().err == (
        "error: unknown property 'src.patern': did you mean 'src.pattern'?\n"
    )
    assert main(["run", "fakesink", "--feature", "nope"]) == 2
    assert capsys.readouterr().err.endswith("or gst:NAME for any GStreamer tracer\n")


def test_ctrl_c_outside_the_play_loop_exits_130_quietly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(*args: object) -> None:
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
    "    gi.version_info = (3, 50, 0)\n"
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


def test_an_unrelated_import_error_is_not_blamed_on_pygobject() -> None:
    code = (
        "import sys\n"
        "sys.modules['mixtape.player'] = None\n"  # `import mixtape.player` raises ImportError
        "from mixtape.cli import main\n"
        "sys.exit(main(['inspect', 'fakesrc ! fakesink']))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert done.returncode == 1
    assert "needs PyGObject" not in done.stderr
    assert "Error: import of mixtape.player halted" in done.stderr, done.stderr


def test_without_pygobject_perf_compare_works(tmp_path: Path) -> None:
    done = without_gi("missing", "perf", "compare", str(tmp_path / "a"), str(tmp_path / "b"))
    assert done.returncode == 2
    assert done.stderr == f"error: {tmp_path / 'a'} does not exist\n"


def test_options_go_anywhere(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ["inspect", "fakesrc", "--expose-all", "!", "fakesink"],
        ["inspect", "--expose-all", "fakesrc", "!", "fakesink"],
        ["inspect", "fakesrc", "!", "fakesink", "--expose-all"],
    ):
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert re.search(r"^fakesrc\d+ \(fakesrc\)", out, re.MULTILINE), argv
        assert re.search(r"^fakesink\d+ \(fakesink\)", out, re.MULTILINE), argv
    # what follows `--` is the description, even a word that looks like an option
    assert main(["inspect", "--", "-weird", "!", "fakesink"]) == 2
    assert capsys.readouterr().err.startswith("error: invalid pipeline '-weird ! fakesink'")


PLAYER_FILE = """\
from mixtape import Player


class Mine(Player):
    description = "fakesrc num-buffers=1 ! fakesink name=sink"


NOT_A_PLAYER = 1
"""


@pytest.fixture
def player_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`tmp_path/sub/myplayer.py`, importable once its folder is put on sys.path."""
    folder = tmp_path / "sub"
    folder.mkdir()
    (folder / "myplayer.py").write_text(PLAYER_FILE, encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))  # `main` adds the folder
    monkeypatch.delitem(sys.modules, "myplayer", raising=False)
    return folder / "myplayer.py"


def test_a_target_can_be_a_file(player_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", f"{player_file}:Mine"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Mine\n") and "sink (fakesink)" in out
    assert str(player_file.parent) in sys.path
    assert main(["run", f"{player_file}:Mine"]) == 0


def test_a_target_file_is_found_from_the_current_folder(
    player_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(player_file.parent.parent)
    assert main(["inspect", "sub/myplayer.py:Mine"]) == 0
    assert capsys.readouterr().out.startswith("Mine\n")


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("{dir}/nothing.py:Mine", "cannot import '{dir}/nothing.py': no such file"),
        ("{dir}/myplayer.py:Nope", "'{dir}/myplayer.py' has no attribute 'Nope'"),
        (
            "{dir}/myplayer.py:NOT_A_PLAYER",
            "{dir}/myplayer.py:NOT_A_PLAYER is not a Player subclass",
        ),
        ("{dir}/json.py:Mine", "'json' is another module"),
    ],
)
def test_a_bad_file_target(
    target: str, error: str, player_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (player_file.parent / "json.py").write_text(PLAYER_FILE, encoding="utf-8")
    shown = {"dir": str(player_file.parent)}
    assert main(["inspect", target.format(**shown)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and error.format(**shown) in err
    assert len(err.splitlines()) == 1


@pytest.mark.parametrize("command", ["run", "inspect"])
def test_a_player_class_takes_no_description_words(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (["tests.players:Demo", "extra", "words"], ["tests.players:Demo extra"]):
        assert main([command, *argv]) == 2
        assert capsys.readouterr().err == (
            "error: tests.players:Demo is a player class and takes no description words; "
            "quote a pipeline description instead\n"
        )


def test_a_bare_module_is_a_description(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", "tests.players"]) == 2
    assert capsys.readouterr().err.startswith("error: invalid pipeline 'tests.players'")


def test_the_feature_options_are_listed_for_an_unknown_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["run", "fakesink", "--feature", "perf,nope=1"]) == 2
    assert capsys.readouterr().err == (
        "error: feature 'perf' has no option 'nope'; its options: out (path), "
        "interval (number > 0)\n"
    )
    assert main(["inspect", "fakesink", "--feature", "perf,interval=0"]) == 2
    assert capsys.readouterr().err == (
        "error: invalid options for feature 'perf': interval: Input should be greater than 0\n"
    )


def test_the_compare_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.test_compare import MS, index, write

    base = write(
        tmp_path / "base",
        "index.json",
        index(a={"pipeline_latency": (10 * MS, 10 * MS)}, gone={"pipeline_latency": (MS, MS)}),
    )
    new = write(
        tmp_path / "new",
        "index.json",
        index(
            a={"pipeline_latency": (40 * MS, 41 * MS), "process_cpu": (90, 90)},
            fresh={"pipeline_latency": (MS, MS)},
        ),
    )
    assert main(["perf", "compare", "--json", "--threshold", "70", str(base), str(new)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["base"] == str(base) and result["new"] == str(new)
    assert result["threshold_percent"] == 70 and "threshold" not in result
    assert result["added"] == [{"test": "fresh"}, {"test": "a", "metric": "process.cpu"}]
    assert result["removed"] == [{"test": "gone"}]
    assert list(result["regressions"][0]) == [
        "test",
        "metric",
        "subject",
        "stat",
        "unit",
        "base",
        "new",
    ]
    assert main(["perf", "compare", "--threshold", "70", str(base), str(new)]) == 1
    assert "(threshold 70%, " in capsys.readouterr().out


def test_version_is_a_bug_report_header(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(":")[0].split(" ")[0] for line in lines] == [
        "mixtape",
        "gstreamer",
        "pygobject",
        "python",
    ]
    assert re.fullmatch(r"gstreamer: 1\.\d+\.\d+(\.\d+)?", lines[1])
    assert re.fullmatch(r"pygobject: \d+\.\d+\.\d+", lines[2])
    assert lines[3] == f"python: {sys.version.split()[0]}"


@pytest.mark.parametrize(
    ("how", "lines"),
    [
        ("missing", ["gstreamer: not found", "pygobject: not found"]),
        ("no-gst", ["gstreamer: not found", "pygobject: 3.50.0"]),
    ],
)
def test_version_without_pygobject(how: str, lines: list[str]) -> None:
    done = without_gi(how, "--version")
    assert done.returncode == 0
    assert done.stdout.splitlines()[1:3] == lines


def test_gst_debug_is_not_coloured_unless_stderr_is_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GST_DEBUG_NO_COLOR", "")  # restored afterwards
    monkeypatch.delenv("GST_DEBUG_NO_COLOR")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    assert main(["--help"]) == 0
    assert "GST_DEBUG_NO_COLOR" not in os.environ
    monkeypatch.setenv("NO_COLOR", "1")
    assert main(["--help"]) == 0
    assert os.environ["GST_DEBUG_NO_COLOR"] == "1"
    monkeypatch.delenv("GST_DEBUG_NO_COLOR")
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    assert main(["--help"]) == 0
    assert os.environ["GST_DEBUG_NO_COLOR"] == "1"
    monkeypatch.setenv("GST_DEBUG_NO_COLOR", "0")  # the user's own choice stays
    assert main(["--help"]) == 0
    assert os.environ["GST_DEBUG_NO_COLOR"] == "0"


@pytest.mark.parametrize(
    ("message", "shown"),
    [
        ("Invalid pipeline 'x'", "invalid pipeline 'x'"),
        ("Could not set the pipeline to null", "could not set the pipeline to null"),
        ("Unknown property 'a.b'", "unknown property 'a.b'"),
        ("Player has no description", "Player has no description"),
        ("Testcard.start() failed", "Testcard.start() failed"),
        ("EOS before the preroll", "EOS before the preroll"),
        ("filesrc0: Resource not found.", "filesrc0: Resource not found."),
    ],
)
def test_error_lowers_a_sentence_and_keeps_a_name(
    message: str, shown: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli._error(message)
    assert capsys.readouterr().err == f"error: {shown}\n"


def test_the_library_sentences_are_all_lowered_by_error() -> None:
    """The first words of the messages the library raises, that `_error` must lower: a new
    one (or a name that starts a message) is a decision, so it fails here."""
    import ast

    package = Path(cli.__file__).parent
    starts = set()
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            first = node.args[0]
            if isinstance(first, ast.JoinedStr) and first.values:
                first = first.values[0]
            if name.endswith("Error") and isinstance(first, ast.Constant):
                starts.add(str(first.value).partition(" ")[0])
    plain = {word for word in starts if word[:1].isupper() and word[1:].islower()}
    assert plain <= cli._SENTENCE_STARTS, plain - cli._SENTENCE_STARTS


def test_help_spells_out_the_target_forms(capsys: pytest.CaptureFixture[str]) -> None:
    for command in ("run", "inspect"):
        assert main([command, "--help"]) == 0
        out = capsys.readouterr().out
        assert out.startswith(f"Usage: mixtape {command} [OPTIONS] TARGET...\n")
        text = " ".join(out.split())
        assert "module:Class" in text and "path/to/file.py:Class" in text


def test_the_threshold_default_is_the_one_of_the_comparison(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mixtape.perf import compare

    assert cli.THRESHOLD_PERCENT == compare.THRESHOLD * 100
    assert main(["perf", "compare", "--help"]) == 0
    assert f"in percent of the base; default {cli.THRESHOLD_PERCENT}" in " ".join(
        capsys.readouterr().out.split()
    )


def test_no_flag_has_a_negative_twin(capsys: pytest.CaptureFixture[str]) -> None:
    for command in (["run"], ["inspect"], ["perf", "compare"]):
        assert main([*command, "--help"]) == 0
        assert "--no-" not in capsys.readouterr().out
