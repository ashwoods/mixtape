"""A pytest plugin that measures every test with GStreamer's own tracers, whatever the suite
does with GStreamer (a `Gst.parse_launch` is enough; no mixtape `Player` needed):

    pytest --mixtape-perf DIR

writes `DIR/<test>/` for each test that ran a pipeline (`summary.json`, `windows.jsonl` and
`report.html`, see `mixtape.perf.Artifacts`) and, at the end, `DIR/index.json` and
`DIR/index.html` with every test's outcome and worst numbers. A test's window is from the start
of its setup to the end of its teardown, as the samples carry the time of the tracers' clock.
The worst p95 and max of each metric go to the test's JUnit properties as
`perf.<metric>.p95` and `perf.<metric>.max`.

Two checks keep a test from getting slower. A budget, `@pytest.mark.perf(pipeline_latency_p95=
"20ms")`, fails the test at the end of its call when the worst subject of the metric is over
its limit (`mixtape.perf.budget`). `--mixtape-perf-baseline BASE` compares the session with an
earlier one at its end (`mixtape.perf.compare`) and fails it on a regression.

Tracers exist only if `Gst.init` creates them, once per process: with the option, the plugin
initialises GStreamer before the first conftest is read, and fails with a usage error when
something did so first. Without the option it does nothing, and imports nothing of GStreamer."""

import hashlib
import json
import logging
import os
import re
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:  # not at run time: it imports GStreamer
    from .perf.budget import Budget
    from .perf.record import Recorder

logger = logging.getLogger(__name__)

_ENV = ("GST_TRACERS", "GST_DEBUG_NO_COLOR")
"""What `prepare` sets for GStreamer: the plugin gives it back to the environment, so that
child processes of the tests do not start tracers of their own"""


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("mixtape-perf", "measure tests with GStreamer's tracers")
    group.addoption(
        "--mixtape-perf",
        metavar="DIR",
        type=Path,
        help="measure each test with GStreamer's tracers: a folder of DIR for each, and an "
        "index of them, DIR/index.html",
    )
    group.addoption(
        "--mixtape-perf-samples",
        action="store_true",
        help="also write every sample of each test (samples.jsonl and a CSV per metric)",
    )
    group.addoption(
        "--mixtape-perf-baseline",
        metavar="BASE",
        type=Path,
        help="compare the session with an earlier one, the DIR of its --mixtape-perf, at the "
        "end: write DIR/compare.json, print the regressions and fail the session on any",
    )
    group.addoption(
        "--mixtape-perf-interval",
        metavar="SECONDS",
        type=float,
        default=1.0,
        help="seconds per window of a running test (default: %(default)s)",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    if early_config.known_args_namespace.mixtape_perf is not None:
        _start_tracers()


def _start_tracers() -> None:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as error:
        raise pytest.UsageError(f"--mixtape-perf needs PyGObject and GStreamer: {error}") from None
    from .exceptions import FeatureError
    from .perf.collect import Collector, prepare

    if Gst.is_initialized():
        raise pytest.UsageError(
            "--mixtape-perf needs GStreamer's tracers, which only Gst.init creates, and "
            "something initialised GStreamer before pytest read its options: a plugin "
            "(`-p`, PYTEST_PLUGINS or an entry point) imports and initialises it first"
        )
    before = {key: os.environ.get(key) for key in _ENV}
    try:
        prepare()
        Gst.init(None)
        Collector.get()
    except FeatureError as error:
        raise pytest.UsageError(f"--mixtape-perf: {error}") from None
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "perf(metric_stat=limit, ...): with --mixtape-perf, fail the test when the worst subject "
        "of a metric is over its limit at the end of the call, e.g. "
        'perf(pipeline_latency_p95="20ms", process_cpu_mean="150%")',
    )
    if config.getoption("mixtape_perf") is not None:
        config.pluginmanager.register(Measure(config), "mixtape-perf")
    elif config.getoption("mixtape_perf_baseline") is not None:
        raise pytest.UsageError("--mixtape-perf-baseline needs --mixtape-perf")


def _folder_name(nodeid: str, taken: set[str]) -> str:
    """A folder name for a test: its id, readable (`tests.test_a.py__test_b[x-1]`) and
    unique."""
    name = re.sub(r"[^\w.\[\]=+@,-]+", "_", nodeid.replace("::", "__").replace("/", "."))
    name = name.lstrip(".") or "test"
    if len(name) > 120:
        name = f"{name[:100]}-{hashlib.sha1(nodeid.encode()).hexdigest()[:10]}"
    unique, count = name, 1
    while unique.casefold() in taken:  # a folder of another case is the same on macOS
        count += 1
        unique = f"{name}-{count}"
    taken.add(unique.casefold())
    return unique


def _worst(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """For each metric of a summary, the unit and the highest p95, max and mean of its subjects
    with the subject that had it."""
    worst: dict[str, dict[str, Any]] = {}
    for metric, found in summary["metrics"].items():
        worst[metric] = {"unit": found["unit"]}
        for stat in ("p95", "max", "mean"):
            subject, top = max(found["subjects"].items(), key=lambda item: item[1][stat])
            worst[metric][stat] = {"value": top[stat], "subject": subject}
    return worst


@dataclass
class _Run:
    """The measuring of the test that runs"""

    item: pytest.Item
    recorder: "Recorder"
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class Measure:
    """The plugin of a session that measures: a `Recorder` per test, whose window a thread
    closes every interval, and the index of the session."""

    def __init__(self, config: pytest.Config) -> None:
        self.out: Path = config.getoption("mixtape_perf").absolute()
        self.samples: bool = config.getoption("mixtape_perf_samples")
        self.interval: float = config.getoption("mixtape_perf_interval")
        if self.interval <= 0:
            raise pytest.UsageError("--mixtape-perf-interval takes a number of seconds above 0")
        self.baseline: Path | None = config.getoption("mixtape_perf_baseline")
        if self.baseline is not None:
            from .perf import compare

            if self.baseline.resolve() == self.out.resolve():
                raise pytest.UsageError(
                    "--mixtape-perf-baseline is the folder of --mixtape-perf, which this "
                    "session replaces: give it a copy of an earlier session"
                )
            try:
                if compare.kind(self.baseline) != "session":
                    raise compare.CompareError(f"{self.baseline} has no index.json")
            except compare.CompareError as error:
                raise pytest.UsageError(f"--mixtape-perf-baseline: {error}") from None
        self._comparison: dict[str, Any] | None = None
        self._comparison_text = ""
        self._budgets: dict[str, list[Budget]] = {}  # by test id
        self._runs: dict[str, _Run] = {}
        self._tests: dict[str, dict[str, Any]] = {}  # the index's entries, by test id
        self._folders: set[str] = set()

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        from .perf import budget

        for item in items:
            keywords: dict[str, Any] = {}
            for mark in item.iter_markers("perf"):  # the closest first
                if mark.args:
                    raise pytest.UsageError(
                        f"{item.nodeid}: the perf marker takes keywords like "
                        f"pipeline_latency_p95='20ms', not {mark.args!r}"
                    )
                for keyword, value in mark.kwargs.items():
                    keywords.setdefault(keyword, value)
            try:
                budgets = budget.parse(keywords)
            except ValueError as error:
                raise pytest.UsageError(f"{item.nodeid}: perf marker {error}") from None
            if budgets:
                self._budgets[item.nodeid] = budgets

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Any:
        from .perf.record import Recorder

        folder = _folder_name(item.nodeid, self._folders)
        run = self._runs[item.nodeid] = _Run(
            item, Recorder(self.out / folder, self.samples, self.interval)
        )
        self._tests[item.nodeid] = {
            "id": item.nodeid,
            "outcome": "passed",
            "duration": 0.0,
            "folder": folder,
            "lost": 0,
            "metrics": {},
        }
        run.thread = threading.Thread(
            target=self._poll, args=(run,), name="mixtape-perf-window", daemon=True
        )
        run.thread.start()
        return (yield)

    def _poll(self, run: _Run) -> None:
        while not run.stop.wait(self.interval):
            try:
                run.recorder.window()
            except Exception:
                logger.exception("perf could not close a window of the running test")
                return

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Any:
        result = yield  # a test that fails by itself keeps its own failure
        run = self._runs.get(item.nodeid)
        if run is not None and (budgets := self._budgets.get(item.nodeid)):
            from .perf import budget

            # the statistics of everything since the setup, as the summary will have them but
            # for the teardown: the recorder keeps every value of the run, which is what a
            # percentile of the whole run needs
            run.recorder.window()
            if breaches := budget.check(budgets, run.recorder.stats()):
                pytest.fail("\n".join(breaches), pytrace=False)
        return result

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> Any:
        try:
            return (yield)
        finally:  # before pytest reports the teardown, which has the properties for JUnit
            if (run := self._runs.pop(item.nodeid, None)) is not None:
                self._finish(run)

    def _finish(self, run: _Run) -> None:
        item = run.item
        run.stop.set()
        if run.thread is not None:
            run.thread.join()
        entry = self._tests[item.nodeid]
        for limit in self._budgets.get(item.nodeid, ()):
            item.user_properties.append((f"perf.budget.{limit.metric}.{limit.stat}", limit.limit))
        recorder = run.recorder
        try:
            recorder.window()
            lost = recorder.lost
        except Exception:
            recorder.discard()
            entry["folder"] = None
            raise
        # only the CPU: nothing ran GStreamer, nothing to show
        if recorder.measured <= {"process.cpu", "thread.cpu"}:
            recorder.discard()
            entry["folder"] = None
        else:
            # `pipeline` is what the report's head and title say of the run: the test's id
            about = {"pipeline": item.nodeid, "test": item.nodeid, "interval": self.interval}
            summary = recorder.finish(about)
            assert summary is not None
            entry["metrics"] = _worst(summary)
            for metric, stats in entry["metrics"].items():
                for stat in ("p95", "max"):
                    item.user_properties.append((f"perf.{metric}.{stat}", stats[stat]["value"]))
        if lost:
            entry["lost"] = lost
            warnings.warn(
                pytest.PytestWarning(
                    f"mixtape perf lost debug log lines ({lost} times) while {item.nodeid} ran: "
                    "some tracer records are missing from its numbers"
                ),
                stacklevel=1,
            )

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        entry = self._tests.get(report.nodeid)
        if entry is None:
            return
        entry["duration"] = round(entry["duration"] + report.duration, 6)
        xfail = hasattr(report, "wasxfail")
        if report.skipped and xfail:  # in the setup too: `pytest.xfail()` in a fixture
            entry["outcome"] = "xfailed"
        elif report.when == "call":
            # a test that passes though it is marked xfail, and not strictly
            entry["outcome"] = "xpassed" if report.passed and xfail else report.outcome
        elif report.failed and entry["outcome"] == "passed":
            entry["outcome"] = "error"
        elif report.skipped and report.when == "setup":
            entry["outcome"] = "skipped"

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        from .perf import compare, index

        # a test that was interrupted has no teardown: finish what it measured, as far as it got
        for run in self._runs.values():
            self._tests[run.item.nodeid]["outcome"] = "interrupted"
            try:
                self._finish(run)
            except Exception:
                logger.exception("perf could not finish %s", run.item.nodeid)
        self._runs.clear()
        index.write(
            self.out,
            {
                "interval": self.interval,
                "samples": self.samples,
                "tests": list(self._tests.values()),
            },
        )
        if self.baseline is not None:
            result = compare.compare_folders(self.baseline, self.out, compare.THRESHOLD)
            self._comparison = result
            self._comparison_text = compare.format_text(result)
            (self.out / "compare.json").write_text(
                json.dumps(self._comparison, indent=2) + "\n", encoding="utf-8"
            )
            if result["regressions"] and session.exitstatus == pytest.ExitCode.OK:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        measured = sum(entry["folder"] is not None for entry in self._tests.values())
        terminalreporter.write_line(
            f"mixtape perf: {measured} of {len(self._tests)} tests measured, "
            f"index at {self.out / 'index.html'}"
        )
        if self._comparison is not None:
            terminalreporter.section("mixtape perf compare")
            terminalreporter.write_line(f"{self.baseline} → {self.out}")
            terminalreporter.write_line(
                self._comparison_text, red=bool(self._comparison["regressions"])
            )
