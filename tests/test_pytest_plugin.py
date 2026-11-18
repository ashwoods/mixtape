"""The pytest plugin, `pytest --mixtape-perf DIR`. Tracers exist once per process, when GStreamer
initialises, so each suite runs in a subprocess of its own."""

import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from mixtape.pytest_plugin import _folder_name

pytestmark = pytest.mark.timeout(90)  # each starts a pytest, and GStreamer, of its own

NO_QUEUE_LEVELS = subprocess.run(
    ["gst-inspect-1.0", "--exists", "queue-levels"], capture_output=True
).returncode
needs_queue_levels = pytest.mark.skipif(
    bool(NO_QUEUE_LEVELS),
    reason="needs the GStreamer tracer queue-levels (gst-plugins-rs)",
)

PLAIN = '''
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst


def run(buffers):
    """A pipeline of plain GStreamer, no mixtape: it runs to its end."""
    pipeline = Gst.parse_launch(
        f"videotestsrc num-buffers={buffers} ! video/x-raw,width=64,height=48 ! queue ! fakesink"
    )
    pipeline.set_state(Gst.State.PLAYING)
    bus = pipeline.get_bus()
    bus.timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipeline.set_state(Gst.State.NULL)
'''

SUITE = (
    PLAIN
    + """
import os


def test_one():
    run(30)


def test_two():
    run(20)


def test_no_pipeline():
    assert 1 + 1 == 2


def test_the_children_do_not_inherit_the_tracers():
    assert "GST_TRACERS" not in os.environ
    assert "GST_DEBUG_NO_COLOR" not in os.environ
"""
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suites start from an environment without the tracers, which other tests of this
    process leave in `GST_TRACERS` when they build a player with perf."""
    for key in ("GST_TRACERS", "GST_DEBUG", "GST_DEBUG_FILE", "GST_DEBUG_NO_COLOR"):
        monkeypatch.delenv(key, raising=False)


def measure(pytester: pytest.Pytester, *args: str) -> tuple[pytest.RunResult, Path]:
    out = pytester.path / "perf"
    result = pytester.runpytest_subprocess("--mixtape-perf", str(out), *args, timeout=120)
    return result, out


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_each_test_gets_a_report_and_the_session_an_index(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_suite=SUITE)
    result, out = measure(pytester, "--junitxml=junit.xml")
    result.assert_outcomes(passed=4)
    index = load(out / "index.json")
    assert index["format_version"] == 1
    tests = {test["id"]: test for test in index["tests"]}
    assert list(tests) == [
        "test_suite.py::test_one",
        "test_suite.py::test_two",
        "test_suite.py::test_no_pipeline",
        "test_suite.py::test_the_children_do_not_inherit_the_tracers",
    ]
    for name, buffers in (("test_one", 30), ("test_two", 20)):
        test = tests[f"test_suite.py::{name}"]
        assert test["outcome"] == "passed" and test["duration"] > 0 and test["lost"] == 0
        folder = out / test["folder"]
        assert (folder / "summary.json").is_file() and (folder / "report.html").is_file()
        assert (folder / "windows.jsonl").is_file()
        assert not (folder / "samples.jsonl").exists() and not list(folder.glob("*.csv"))
        summary = load(folder / "summary.json")
        assert summary["test"] == f"test_suite.py::{name}" and summary["interval"] == 1.0
        latency = summary["metrics"]["element.latency"]["subjects"]
        (queue,) = (stats for subject, stats in latency.items() if subject.startswith("queue"))
        assert queue["n"] == buffers
        worst = test["metrics"]["element.latency"]
        assert worst["unit"] == "ns" and worst["p95"]["subject"].startswith("queue")
        assert worst["p95"]["value"] == queue["p95"]
        # the queue holds the buffers, but on a busy machine a preempted element can have the max
        (subject, stats), *_ = sorted(latency.items(), key=lambda item: -item[1]["max"])
        assert worst["max"] == {"subject": subject, "value": stats["max"]}, (worst, latency)
        assert test["metrics"]["process.cpu"]["mean"]["subject"] == "process"
    # a test that ran no pipeline is listed, with no folder
    assert tests["test_suite.py::test_no_pipeline"]["folder"] is None
    assert tests["test_suite.py::test_no_pipeline"]["metrics"] == {}
    assert sorted(path.name for path in out.iterdir() if path.is_dir()) == sorted(
        tests[f"test_suite.py::{name}"]["folder"] for name in ("test_one", "test_two")
    )
    page = (out / "index.html").read_text(encoding="utf-8")
    for test in tests.values():
        assert test["id"] in page
    assert f'href="{tests["test_suite.py::test_one"]["folder"]}/report.html"' in page
    assert "default-src 'none'" in page and "<script" not in page
    # JUnit gets the worst p95 and max of each metric
    properties = {
        prop.get("name", ""): prop.get("value", "")
        for case in ET.parse(pytester.path / "junit.xml").iter("testcase")
        if case.get("name") == "test_one"
        for prop in case.iter("property")
    }
    worst = tests["test_suite.py::test_one"]["metrics"]
    assert float(properties["perf.element.latency.p95"]) == worst["element.latency"]["p95"]["value"]
    assert float(properties["perf.process.cpu.max"]) == worst["process.cpu"]["max"]["value"]
    assert {name for name in properties if name.startswith("perf.pipeline.latency")} == {
        "perf.pipeline.latency.p95",
        "perf.pipeline.latency.max",
    }
    result.stdout.fnmatch_lines(["mixtape perf: 2 of 4 tests measured, index at *index.html"])


def test_samples_are_written_only_when_asked(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_suite=PLAIN + "\ndef test_one():\n    run(10)\n")
    result, out = measure(pytester, "--mixtape-perf-samples", "--mixtape-perf-interval=0.5")
    result.assert_outcomes(passed=1)
    folder = out / load(out / "index.json")["tests"][0]["folder"]
    assert (folder / "samples.jsonl").stat().st_size > 0
    assert (folder / "element.latency.csv").stat().st_size > 0
    assert load(out / "index.json")["interval"] == 0.5


def test_nothing_of_gstreamer_is_imported_or_written_without_the_option(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        test_suite="""
        import os
        import sys

        import pytest


        @pytest.mark.perf(pipeline_latency_p95="1ns", nope=1)  # known, and ignored
        def test_it():
            assert not {"gi", "mixtape.perf.collect"} & set(sys.modules)
            assert "GST_TRACERS" not in os.environ
        """
    )
    result = pytester.runpytest_subprocess("--strict-markers", timeout=60)
    result.assert_outcomes(passed=1)
    assert not [p for p in pytester.path.rglob("*") if p.name.startswith(("index", "summary"))]
    assert "mixtape perf" not in result.stdout.str()


def test_gstreamer_initialised_before_the_plugin_is_a_usage_error(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        early="""
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        """
    )
    pytester.makepyfile(test_suite="def test_it():\n    pass\n")
    result = pytester.runpytest_subprocess(
        "-p", "early", "--mixtape-perf", str(pytester.path / "perf"), timeout=60
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*--mixtape-perf needs GStreamer's tracers*initialised GStreamer*"]
    )
    assert not (pytester.path / "perf").exists()


def test_a_session_with_xdist_workers_is_a_usage_error(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_suite="def test_it():\n    pass\n")
    result, out = measure(pytester, "-n", "2")  # pytest-xdist, a dev dependency
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--mixtape-perf measures a session run serially*without -n*"])
    assert not out.exists()


def test_a_perf_player_and_the_plugin_both_get_the_samples(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_suite="""
        import asyncio
        import json

        from mixtape import Player


        def test_player(tmp_path):
            async def main():
                player = Player.from_description(
                    "videotestsrc num-buffers=30 ! video/x-raw,width=64,height=48 "
                    "! queue name=q ! fakesink",
                    features=[f"perf,interval=0.2,out={tmp_path}"],
                )
                async with player:
                    await player.play_until_eos()

            asyncio.run(main())
            summary = json.loads((tmp_path / "summary.json").read_text())
            assert summary["metrics"]["element.latency"]["subjects"]["q"]["n"] == 30
        """
    )
    result, out = measure(pytester)
    result.assert_outcomes(passed=1)
    test = load(out / "index.json")["tests"][0]
    summary = load(out / test["folder"] / "summary.json")
    assert summary["metrics"]["element.latency"]["subjects"]["q"]["n"] == 30
    assert summary["metrics"]["pipeline.latency"]["subjects"]["videotestsrc0→fakesink0"]["n"] == 30
    assert test["metrics"]["element.latency"]["max"]["subject"] == "q"


def test_markup_in_a_test_id_is_text_on_the_page(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_suite=PLAIN
        + """
import pytest


@pytest.mark.parametrize("name", ["<script>x</script>", "a/b c"])
def test_it(name):
    run(5)
"""
    )
    result, out = measure(pytester)
    result.assert_outcomes(passed=2)
    index = load(out / "index.json")
    folders = [test["folder"] for test in index["tests"]]
    assert len(set(folders)) == 2 and all("<" not in f and " " not in f for f in folders)
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "<script" not in page and "&lt;script&gt;x&lt;/script&gt;" in page


BUDGETS = (
    PLAIN
    + """
import pytest


@pytest.mark.perf(pipeline_latency_p95="1ns", process_cpu_mean="100000%")
def test_tight():
    run(20)


@pytest.mark.perf(pipeline_latency_p95="10s", queue_fill_max=1, process_cpu_mean="100000%")
def test_generous():
    run(20)


@pytest.mark.perf(pipeline_latency_max="1ns")
def test_the_test_fails_by_itself():
    run(5)
    assert False, "its own"


def test_no_budget():
    run(5)
"""
)


@needs_queue_levels  # test_generous has a budget on queue.fill
def test_a_budget_fails_the_test_that_is_over_it(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_suite=BUDGETS)
    result, out = measure(pytester, "--junitxml=junit.xml")
    result.assert_outcomes(passed=2, failed=2)  # a failure, not an error in the teardown
    result.stdout.fnmatch_lines(
        ["*perf budget: pipeline.latency p95 is * on videotestsrc0→fakesink0, over 1 ns*"]
    )
    result.stdout.fnmatch_lines(["*AssertionError: its own*"])  # its own failure, not the budget
    outcomes = {test["id"]: test["outcome"] for test in load(out / "index.json")["tests"]}
    assert outcomes["test_suite.py::test_tight"] == "failed"
    assert outcomes["test_suite.py::test_generous"] == "passed"
    properties = {
        case.get("name"): {p.get("name"): p.get("value") for p in case.iter("property")}
        for case in ET.parse(pytester.path / "junit.xml").iter("testcase")
    }
    assert properties["test_tight"]["perf.budget.pipeline.latency.p95"] == "1.0"
    assert properties["test_tight"]["perf.budget.process.cpu.mean"] == "100000.0"
    assert properties["test_generous"]["perf.budget.queue.fill.max"] == "1.0"
    assert not [name for name in properties["test_no_budget"] if "budget" in (name or "")]


def test_a_bad_budget_is_a_usage_error(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_suite="""
        import pytest


        @pytest.mark.perf(nope_p95=1)
        def test_it():
            pass
        """
    )
    result, _ = measure(pytester)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*test_suite.py::test_it: perf marker nope_p95: unknown metric*"])


def baseline(path: Path, value: float) -> Path:
    """A session in which `test_one` had `value` ns of pipeline latency, and a test that is no
    more."""
    worst = {"unit": "ns", **{stat: {"value": value, "subject": "a→b"} for stat in ("p95", "max")}}
    test = {"outcome": "passed", "duration": 1.0, "folder": None, "lost": 0}
    index = {
        "interval": 1.0,
        "samples": False,
        "tests": [
            {"id": "test_suite.py::test_one", **test, "metrics": {"pipeline.latency": worst}},
            {"id": "test_suite.py::test_old", **test, "metrics": {}},
        ],
    }
    path.mkdir()
    (path / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return path


@pytest.mark.parametrize(("value", "regressed"), [(1, True), (1e12, False)])
def test_a_session_is_compared_with_its_baseline(
    pytester: pytest.Pytester, value: float, regressed: bool
) -> None:
    pytester.makepyfile(test_suite=PLAIN + "\ndef test_one():\n    run(20)\n")
    base = baseline(pytester.path / "base", value)
    result, out = measure(pytester, f"--mixtape-perf-baseline={base}")
    comparison = load(out / "compare.json")
    assert comparison["base"] == str(base) and comparison["new"] == str(out)
    assert comparison["removed"] == [{"test": "test_suite.py::test_old"}]
    assert comparison["threshold_percent"] == 50
    if regressed:
        assert result.ret == pytest.ExitCode.TESTS_FAILED
        result.assert_outcomes(passed=1)  # the tests did not fail, the session did
        assert {c["test"] for c in comparison["regressions"]} == {"test_suite.py::test_one"}
        result.stdout.fnmatch_lines(
            [
                "*mixtape perf compare*",
                "Regressions:",
                "*test_suite.py::test_one  pipeline.latency  p95*videotestsrc0→fakesink0*",
            ]
        )
    else:
        assert result.ret == pytest.ExitCode.OK
        assert comparison["regressions"] == [] and comparison["improvements"]


def test_a_baseline_needs_the_option_and_a_session(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_suite="def test_it():\n    pass\n")
    result = pytester.runpytest_subprocess("--mixtape-perf-baseline=nowhere", timeout=60)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--mixtape-perf-baseline needs --mixtape-perf*"])
    result, _ = measure(pytester, "--mixtape-perf-baseline=nowhere")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--mixtape-perf-baseline: *nowhere does not exist*"])
    # the folder of this session is no baseline: it is replaced as the session goes
    result, out = measure(pytester, f"--mixtape-perf-baseline={pytester.path / 'perf' / '.'}")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--mixtape-perf-baseline is the folder of --mixtape-perf*"])


def test_folders_differ_even_where_the_file_system_ignores_case() -> None:
    taken: set[str] = set()
    names = [_folder_name(f"t.py::test_x[{param}]", taken) for param in ("A", "a", "a")]
    assert names[0] == "t.py__test_x[A]"
    assert len({name.casefold() for name in names}) == 3


def test_an_interrupted_test_is_listed_as_interrupted_with_what_it_measured(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        test_suite=PLAIN
        + """
import pytest


def test_first():
    run(10)


def test_stops():
    run(10)
    pytest.exit("enough")


def test_never():
    run(10)
"""
    )
    result, out = measure(pytester)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    tests = {test["id"]: test for test in load(out / "index.json")["tests"]}
    assert list(tests) == ["test_suite.py::test_first", "test_suite.py::test_stops"]
    assert [test["outcome"] for test in tests.values()] == ["passed", "interrupted"]
    stops = tests["test_suite.py::test_stops"]
    assert (out / stops["folder"] / "report.html").is_file()  # no link to what is not there
    assert stops["metrics"]["element.latency"]["p95"]["value"] > 0
