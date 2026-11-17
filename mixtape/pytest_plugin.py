"""A pytest plugin that measures every test with GStreamer's own tracers, whatever the suite
does with GStreamer (a `Gst.parse_launch` is enough; no mixtape `Player` needed):

    pytest --mixtape-perf DIR

writes `DIR/<test>/` for each test that ran a pipeline (`summary.json`, `windows.jsonl` and
`report.html`, see `mixtape.perf.Artifacts`) and, at the end, `DIR/index.json` and
`DIR/index.html` with every test's outcome and worst numbers. A test's window is from the start
of its setup to the end of its teardown, as the samples carry the time of the tracers' clock.
The worst p95 and max of each metric go to the test's JUnit properties as
`perf.<metric>.p95` and `perf.<metric>.max`.

Tracers exist only if `Gst.init` creates them, once per process: with the option, the plugin
initialises GStreamer before the first conftest is read, and fails with a usage error when
something did so first. Without the option it does nothing, and imports nothing of GStreamer."""

import hashlib
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
    if config.getoption("mixtape_perf") is not None:
        config.pluginmanager.register(Measure(config), "mixtape-perf")


def _folder_name(nodeid: str, taken: set[str]) -> str:
    """A folder name for a test: its id, readable (`tests.test_a.py__test_b[x-1]`) and
    unique."""
    name = re.sub(r"[^\w.\[\]=+@,-]+", "_", nodeid.replace("::", "__").replace("/", "."))
    name = name.lstrip(".") or "test"
    if len(name) > 120:
        name = f"{name[:100]}-{hashlib.sha1(nodeid.encode()).hexdigest()[:10]}"
    unique, count = name, 1
    while unique in taken:
        count += 1
        unique = f"{name}-{count}"
    taken.add(unique)
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
        self._runs: dict[str, _Run] = {}
        self._tests: dict[str, dict[str, Any]] = {}  # the index's entries, by test id
        self._folders: set[str] = set()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Any:
        from .perf.record import Recorder

        folder = _folder_name(item.nodeid, self._folders)
        run = self._runs[item.nodeid] = _Run(Recorder(self.out / folder, self.samples))
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
    def pytest_runtest_teardown(self, item: pytest.Item) -> Any:
        try:
            return (yield)
        finally:  # before pytest reports the teardown, which has the properties for JUnit
            if (run := self._runs.pop(item.nodeid, None)) is not None:
                self._finish(item, run)

    def _finish(self, item: pytest.Item, run: _Run) -> None:
        run.stop.set()
        if run.thread is not None:
            run.thread.join()
        entry = self._tests[item.nodeid]
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
        if report.when == "call":
            if report.skipped and hasattr(report, "wasxfail"):
                entry["outcome"] = "xfailed"
            else:
                entry["outcome"] = report.outcome
        elif report.failed and entry["outcome"] == "passed":
            entry["outcome"] = "error"
        elif report.skipped and report.when == "setup":
            entry["outcome"] = "skipped"

    def pytest_sessionfinish(self) -> None:
        from .perf import index

        index.write(
            self.out,
            {
                "interval": self.interval,
                "samples": self.samples,
                "tests": list(self._tests.values()),
            },
        )

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        measured = sum(entry["folder"] is not None for entry in self._tests.values())
        terminalreporter.write_line(
            f"mixtape perf: {measured} of {len(self._tests)} tests measured, "
            f"index at {self.out / 'index.html'}"
        )
