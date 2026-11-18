# type: ignore
import logging
import os

import colorlog
import pytest

from tests.flaky import Timing

pytest_plugins = ["pytester"]

logger = logging.getLogger(__name__)
handler = colorlog.StreamHandler()
handler.setFormatter(
    colorlog.ColoredFormatter(
        "(%(asctime)s) [%(log_color)s%(levelname)s] | %(name)s | %(message)s [%(threadName)-10s]"
    )
)

# get root logger
logger = logging.getLogger()
logger.handlers = []
logger.addHandler(handler)
logger.setLevel(logging.INFO)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "covers(*sections): the example sections (name:section, as in recorder:build) whose"
        " code this test exercises; docs/ shows it under the snippet (hooks/source_lines.py)",
    )
    config.addinivalue_line(
        "markers",
        "flaky(reason): a bound this test measures with tests.flaky.timing() can break on a busy"
        " machine; when one does, the test is an xfail with its message (CONTRIBUTING.md)",
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """A test marked flaky whose timing() bound broke is an xfail, as pytest.xfail() makes one:
    `x` in the progress and XFAIL with the bound's message in the summary."""
    report = yield
    marker = item.get_closest_marker("flaky")
    if marker and report.when == "call" and report.failed and call.excinfo.errisinstance(Timing):
        first = str(call.excinfo.value).strip().splitlines()[:1]
        report.outcome = "skipped"
        report.wasxfail = f"flaky, {marker.kwargs['reason']}: {' '.join(first)}"
    return report


def pytest_terminal_summary(terminalreporter):
    """On GitHub Actions, each broken bound is a warning on the run's page too."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for report in terminalreporter.stats.get("xfailed", []):
        if getattr(report, "wasxfail", "").startswith("flaky, "):
            terminalreporter.write_line(
                f"::warning title=flaky::{report.nodeid}: {report.wasxfail}"
            )


@pytest.fixture
def Gst():  # noqa
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst as GstCls

    GstCls.init(None)
    return GstCls


@pytest.fixture
def pipeline(Gst):
    """Make sure test pipeline is correct and test env setup"""
    description = "videotestsrc ! queue ! fakesink"
    pipeline = Gst.parse_launch(description)
    assert isinstance(pipeline, Gst.Pipeline)
    return pipeline


@pytest.fixture
def error_pipeline(Gst):
    """Error pipeline"""
    description = "filesrc ! queue ! fakesink"
    pipeline = Gst.parse_launch(description)
    assert isinstance(pipeline, Gst.Pipeline)
    return pipeline
