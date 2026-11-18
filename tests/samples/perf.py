"""The samples of perf.md: see tests/doc_samples.py.

Every number the page states is checked against a bound: the latency of the slow recorder is
seconds because x264 looks ahead tens of frames of a live source, whatever the CPU, and the fast
one is tens of milliseconds. The bounds hold for the page; a busy machine can break them in a
run, so they are timing() bounds and the samples that run x264 are flaky (tests/flaky.py)."""

import re

from tests.doc_samples import RUST_TRACERS, Sample, not_run, run
from tests.flaky import timing

PAGE = "perf.md"
ENCODER = ("x264enc",)
TRACERS = ("x264enc", "queue-levels")  # the page reads the queue's fill

# why the samples that encode with x264 are flaky
X264 = "x264's latency and CPU share depend on the machine's load"
# what pytest-timeout prints as it ends a run of examples/recorder_suite whose test ran over
# this project's 20 s (pyproject.toml): it ran out of time, a broken bound (tests/flaky.py)
TIMEOUT = "+ Timeout +"


def over_budgets(out: str) -> bool:
    """Whether every failure of a run of examples/recorder_suite is a budget's."""
    report = out.partition(" FAILURES ")[2].partition(" short test summary info ")[0]
    failures = re.split(r"^_{3,} \S+ _{3,}$", report, flags=re.M)[1:]
    return bool(failures) and all("perf budget: " in f for f in failures)


UNITS = {"ns": 1e-6, "µs": 1e-3, "ms": 1.0, "s": 1000.0}


def ms(text: str) -> float:
    """A duration as the page writes it (`9.4s`, `73 ms`, `111 µs`) in milliseconds."""
    found = re.fullmatch(r"([\d.]+) ?(ns|µs|ms|s)", text)
    assert found, f"not a duration: {text!r}"
    return float(found[1]) * UNITS[found[2]]


def worst(out: str) -> None:
    """The table of examples/perf_worst.py, for the slow recorder."""
    rows = {line.split()[0]: line.split() for line in out.splitlines()[1:]}
    assert list(rows) == [
        "element.latency",
        "pipeline.latency",
        "queue.fill",
        "process.cpu",
        "thread.cpu",
    ], out
    assert rows["element.latency"][1] == "enc", out
    assert rows["pipeline.latency"][1] == "src→sink", out
    assert rows["thread.cpu"][1] == "q:src", out
    # the encoder holds a buffer for seconds, the queue in front of it is full, and its thread
    # is busy
    timing(ms(rows["element.latency"][2]) > 1000, f"element.latency p95 under 1 s\n{out}")
    timing(ms(rows["pipeline.latency"][2]) > 1000, f"pipeline.latency p95 under 1 s\n{out}")
    timing(float(rows["queue.fill"][2].rstrip("%")) >= 90, f"queue.fill p95 under 90%\n{out}")
    timing(float(rows["thread.cpu"][2].rstrip("%")) >= 30, f"thread.cpu p95 under 30%\n{out}")


def compared(out: str) -> None:
    """The comparison of the slow recorder with the fast one: the latency is far lower, and
    the exit status says whether anything got slower."""
    regressions = re.search(r"(\d+) regressions", out)
    status = re.search(r"exit status (\d+)", out)
    assert regressions and status, out
    # a busy machine can add the latency maximum of an element to the regressions: then it exits 1
    assert (status[1] == "0") == (regressions[1] == "0"), out
    improvements = out.partition("Improvements:")[2]
    for metric, subject in (("pipeline.latency", "src→sink"), ("element.latency", "enc")):
        line = rf"{re.escape(metric)}\s+p95\s+{subject}\s+(\S+ \S+) → (\S+ \S+)"
        found = re.search(line, improvements)
        timing(found, f"no {metric} p95 of {subject} in the improvements\n{out}")
        assert found
        slow, fast = ms(found[1]), ms(found[2])
        timing(slow > 1000 and fast < slow / 3, f"{metric} p95 {found[1]} → {found[2]}\n{out}")


def junit(out: str) -> None:
    summary = re.search(r"^(\d+ (?:passed|failed).*) in [\d.]+s$", out, re.M)
    assert summary, out
    if summary[1] != "2 passed":
        timing(not over_budgets(out), f"{summary[1]}, not 2 passed\n{out}")
    assert summary[1] == "2 passed", out
    names = re.findall(r'name="([^"]+)"', out[summary.end() :])
    assert names == [
        "test_records_720p",
        "perf.budget.pipeline.latency.p95",
        "perf.budget.process.cpu.mean",
        "perf.pipeline.latency.p95",
        "perf.pipeline.latency.max",
        "test_records_360p",
        "perf.pipeline.latency.p95",
        "perf.pipeline.latency.max",
    ], out
    values = re.findall(r'"perf\.[a-z0-9.]+" value="([^"]+)"', out)
    assert [float(v) for v in values[:2]] == [500_000_000.0, 150.0], out
    # the budget is half a second: the 720p test passed, so its p95 is under it
    timing(float(values[2]) < 500_000_000, f"pipeline.latency p95 of 720p over 500 ms\n{out}")


def over_budget(out: str) -> None:
    found = re.search(
        r"perf budget: pipeline\.latency p95 is (\S+ \S+) on src→sink, over 500 ms", out
    )
    assert found, out
    assert "1 failed, 1 passed" in out, out
    timing(ms(found[1]) > 1000, f"pipeline.latency p95 {found[1]}, under 1 s\n{out}")


def baseline(out: str) -> None:
    assert re.search(r"\d+ regressions, \d+ improvements, 0 added, 1 removed", out), out
    regressions = out.partition("Regressions:")[2].partition("\n\n")[0]
    found = re.search(r"pipeline\.latency\s+p95\s+\S+\s+(\S+ \S+) → (\S+ \S+)", regressions)
    timing(found, f"no pipeline.latency p95 in the regressions\n{out}")
    assert found
    timing(
        ms(found[1]) < 500 and ms(found[2]) > 1000,
        f"pipeline.latency p95 {found[1]} → {found[2]}, not under 500 ms → over 1 s\n{out}",
    )
    timing("exit status 1" in out, f"no regression: exit status 0\n{out}")


SAMPLES: list[Sample] = [
    run(
        PAGE,
        "record-and-read",
        "perf_worst.py /tmp/perf-slow",
        elements=TRACERS,
        timeout=120,
        expect=("metric", "element.latency", "pipeline.latency", "queue.fill", "thread.cpu"),
        check=worst,
        check_shown=True,
        flaky=X264,
    ),
    run(
        PAGE,
        "compare",
        "mixtape perf compare /tmp/perf-slow",
        elements=TRACERS,
        timeout=120,
        expect=("exit status",),
        check=compared,
        check_shown=True,
        flaky=X264,
    ),
    not_run(PAGE, "webcam-macos", "avfvideosrc", "needs a camera"),
    not_run(PAGE, "webcam-linux", "v4l2src", "needs a camera"),
    not_run(PAGE, "rtsp", "rtspsrc", "needs an RTSP server"),
    run(
        PAGE,
        "live",
        "--feature perf --http 8047",
        elements=(*ENCODER, *RUST_TRACERS),  # perf's warning of a missing one is in the output
        hangs=True,
        timeout=10,
        expect=("Serving on http://127.0.0.1:8047",),
        exact=True,
    ),
    run(PAGE, "late", "python3 examples/perf_late.py", exact=True),
    run(
        PAGE,
        "suite",
        "--junitxml=/tmp/junit.xml",
        elements=TRACERS,
        timeout=120,
        expect=("mixtape perf: 2 of 2 tests measured",),
        check=junit,
        check_shown=True,
        flaky=X264,
        out_of_time=TIMEOUT,
    ),
    run(
        PAGE,
        "budget-fails",
        "/tmp/perf-slow-out",
        elements=TRACERS,
        status=1,
        timeout=120,
        expect=("mixtape perf: 2 of 2 tests measured", "FAILED examples/recorder_suite/"),
        check=over_budget,
        check_shown=True,
        flaky=X264,
        out_of_time=TIMEOUT,
    ),
    run(
        PAGE,
        "baseline",
        "--mixtape-perf-baseline",
        elements=TRACERS,
        timeout=120,
        expect=("mixtape perf compare", "Removed:", "1 passed, 1 deselected"),
        check=baseline,
        check_shown=True,
        flaky=X264,
        out_of_time=TIMEOUT,
    ),
]
