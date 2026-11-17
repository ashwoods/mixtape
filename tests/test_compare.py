"""`mixtape perf compare` and the budgets of `@pytest.mark.perf`, on documents made by hand."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from mixtape.cli import main
from mixtape.perf import budget, compare
from mixtape.perf.aggregate import RunStat

MS = 1e6


def index(**tests: dict[str, tuple[float, float]]) -> dict[str, Any]:
    """An `index.json` of tests with the (p95, max) of their `pipeline.latency` (ns) and the
    others by metric name, `process_cpu` for `process.cpu`."""
    return {
        "interval": 1.0,
        "samples": False,
        "tests": [
            {
                "id": name,
                "outcome": "passed",
                "duration": 1.0,
                "folder": name,
                "lost": 0,
                "metrics": {
                    metric.replace("_", "."): {
                        "unit": {"process_cpu": "%", "pad_buffers": "buffers/s"}.get(metric, "ns"),
                        "p95": {"value": p95, "subject": "x"},
                        "max": {"value": top, "subject": "x"},
                        "mean": {"value": p95, "subject": "x"},
                    }
                    for metric, (p95, top) in metrics.items()
                },
            }
            for name, metrics in tests.items()
        ],
    }


def summary(metric: str, unit: str, **subjects: tuple[float, float]) -> dict[str, Any]:
    return {
        "metrics": {
            metric: {
                "unit": unit,
                "subjects": {
                    subject: {"n": 9, "mean": p95, "p50": p95, "p95": p95, "p99": top, "max": top}
                    for subject, (p95, top) in subjects.items()
                },
            }
        }
    }


def test_a_session_has_regressions_improvements_and_what_is_added() -> None:
    base = index(
        slow={"pipeline_latency": (10 * MS, 12 * MS)},
        tiny={"pipeline_latency": (50_000, 60_000)},
        fast={"pipeline_latency": (20 * MS, 20 * MS), "process_cpu": (50, 50)},
        steady={"pipeline_latency": (10 * MS, 10 * MS), "pad_buffers": (30, 30)},
        gone={"pipeline_latency": (1 * MS, 1 * MS)},
        # a metric of a newer version is not compared, nor does it fail
        future={"pipeline_latency": (1 * MS, 1 * MS), "future_metric": (1, 100 * MS)},
        # the push time of a short test swings by tens of ms: its floor is 50 ms
        push={"pad_push": (10 * MS, 10 * MS)},
    )
    new = index(
        slow={"pipeline_latency": (16 * MS, 12 * MS)},  # +60% and +6 ms: p95 regresses
        tiny={"pipeline_latency": (150_000, 160_000)},  # +200%, but only 100 µs: under the floor
        fast={"pipeline_latency": (10 * MS, 10 * MS), "process_cpu": (50, 52)},
        steady={"pipeline_latency": (11 * MS, 10 * MS), "pad_buffers": (3, 3)},  # +10%
        fresh={"pipeline_latency": (1 * MS, 1 * MS)},
        future={"pipeline_latency": (1 * MS, 1 * MS), "future_metric": (100 * MS, 1)},
        push={"pad_push": (40 * MS, 40 * MS)},  # +300%, but 30 ms
    )
    result = compare.compare(base, new, "session")
    assert [
        (c["test"], c["metric"], c["stat"], c["base"], c["new"]) for c in result["regressions"]
    ] == [("slow", "pipeline.latency", "p95", 10 * MS, 16 * MS)]
    assert result["regressions"][0]["unit"] == "ns" and result["regressions"][0]["subject"] == "x"
    assert [(c["test"], c["stat"]) for c in result["improvements"]] == [
        ("fast", "p95"),
        ("fast", "max"),
    ]
    assert result["added"] == [{"test": "fresh"}] and result["removed"] == [{"test": "gone"}]
    text = compare.format_text(result)
    assert "slow  pipeline.latency  p95" in text and "10 ms → 16 ms  +60%" in text
    assert text.splitlines()[-1].startswith("1 regressions, 2 improvements, 1 added, 1 removed")
    assert "tiny" not in text and "buffers" not in text


def test_a_metric_one_side_lacks_is_added_not_a_regression() -> None:
    base = index(a={"pipeline_latency": (1 * MS, 1 * MS)})
    new = index(a={"pipeline_latency": (1 * MS, 1 * MS), "process_cpu": (90, 90)})
    result = compare.compare(base, new, "session")
    assert result["regressions"] == [] and result["added"] == [
        {"test": "a", "metric": "process.cpu"}
    ]


def test_a_run_compares_each_subject() -> None:
    base = summary("element.latency", "ns", q=(1 * MS, 2 * MS), u=(1 * MS, 2 * MS), v=(1, 1))
    new = summary("element.latency", "ns", q=(2 * MS, 2 * MS), w=(1, 1), v=(1, 1))
    result = compare.compare(base, new, "run")
    assert [(c["subject"], c["stat"]) for c in result["regressions"]] == [("q", "p95")]
    assert result["added"] == [{"metric": "element.latency", "subject": "w"}]
    assert result["removed"] == [{"metric": "element.latency", "subject": "u"}]
    assert result["compared"] == 4  # q and v, p95 and max
    # a metric of a newer version is not compared, nor does it fail
    assert compare.compare(
        summary("future.metric", "ns", a=(1, 1)),
        summary("future.metric", "ns", a=(9e9, 9e9)),
        "run",
    ) == compare.compare({"metrics": {}}, {"metrics": {}}, "run")
    # a metric where higher is no worse is not compared
    assert (
        compare.compare(
            summary("pad.buffers", "buffers/s", a=(1, 1)),
            summary("pad.buffers", "buffers/s", a=(100, 100)),
            "run",
        )["compared"]
        == 0
    )
    cpu = compare.compare(
        summary("process.cpu", "%", process=(5, 60)),
        summary("process.cpu", "%", process=(9, 100)),
        "run",
    )
    assert [c["stat"] for c in cpu["regressions"]] == ["max"]  # +80% p95 is 4 points: under 5
    queue = compare.compare(
        summary("queue.fill", "ratio", q=(0.5, 0.5)),
        summary("queue.fill", "ratio", q=(0.9, 0.52)),
        "run",
    )
    assert [c["stat"] for c in queue["regressions"]] == ["p95"]


def test_the_threshold_decides() -> None:
    base, new = (
        index(a={"pipeline_latency": (10 * MS, 10 * MS)}),
        index(a={"pipeline_latency": (12 * MS, 10 * MS)}),
    )
    assert not compare.compare(base, new, "session")["regressions"]  # +20% of 50%
    assert compare.compare(base, new, "session", threshold=0.1)["regressions"]


def write(folder: Path, name: str, document: dict[str, Any]) -> Path:
    folder.mkdir()
    (folder / name).write_text(json.dumps(document), encoding="utf-8")
    return folder


def test_the_command_exits_1_on_a_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = write(tmp_path / "base", "index.json", index(a={"pipeline_latency": (10 * MS, 10 * MS)}))
    same = write(tmp_path / "same", "index.json", index(a={"pipeline_latency": (10 * MS, 10 * MS)}))
    worse = write(
        tmp_path / "worse", "index.json", index(a={"pipeline_latency": (40 * MS, 41 * MS)})
    )
    assert main(["perf", "compare", str(base), str(same)]) == 0
    assert capsys.readouterr().out.strip().startswith("0 regressions")
    assert main(["perf", "compare", str(base), str(worse)]) == 1
    assert "a  pipeline.latency  p95" in capsys.readouterr().out
    assert main(["perf", "compare", "--threshold", "500", str(base), str(worse)]) == 0
    capsys.readouterr()
    assert main(["perf", "compare", "--json", str(base), str(worse)]) == 1
    assert json.loads(capsys.readouterr().out)["kind"] == "session"


def test_the_command_compares_single_runs_and_refuses_what_it_cannot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = write(tmp_path / "base", "summary.json", summary("element.latency", "ns", q=(MS, MS)))
    new = write(tmp_path / "new", "summary.json", summary("element.latency", "ns", q=(9 * MS, MS)))
    session = write(tmp_path / "session", "index.json", index())
    assert main(["perf", "compare", "--json", str(base), str(new)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "run" and result["regressions"][0]["subject"] == "q"
    assert main(["perf", "compare", str(base), str(session)]) == 2
    assert "compare two sessions or two runs" in capsys.readouterr().err
    assert main(["perf", "compare", str(base), str(tmp_path / "missing")]) == 2
    assert capsys.readouterr().err == f"error: {tmp_path / 'missing'} does not exist\n"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["perf", "compare", str(base), str(empty)]) == 2
    err = capsys.readouterr().err
    assert "neither an index.json nor a summary.json" in err
    assert "perf,out=DIR" in err and "pytest --mixtape-perf DIR" in err


# -- budgets -- #


@pytest.mark.parametrize(
    ("keyword", "value", "metric", "stat", "limit"),
    [
        ("pipeline_latency_p95", "20ms", "pipeline.latency", "p95", 20 * MS),
        ("pipeline_latency_p99", "1.5 s", "pipeline.latency", "p99", 1.5e9),
        ("element_latency_max", "250µs", "element.latency", "max", 250e3),
        ("element_latency_max", "250us", "element.latency", "max", 250e3),
        ("pad_push_mean", "40ns", "pad.push", "mean", 40),
        ("pad_push_mean", 40, "pad.push", "mean", 40),
        ("process_cpu_mean", "150%", "process.cpu", "mean", 150),
        ("thread_cpu_max", 80.5, "thread.cpu", "max", 80.5),
        ("queue_fill_max", "90%", "queue.fill", "max", 0.9),
        ("queue_fill_max", 0.9, "queue.fill", "max", 0.9),
        ("pad_lateness_p50", "-2ms", "pad.lateness", "p50", -2 * MS),
        ("queue_buffers_n", 100, "queue.buffers", "n", 100),
    ],
)
def test_a_budget_is_a_metric_a_stat_and_a_limit(
    keyword: str, value: Any, metric: str, stat: str, limit: float
) -> None:
    assert budget.parse({keyword: value}) == [budget.Budget(metric, stat, limit)]


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    [
        ("nope_p95", 1, "unknown metric 'nope'"),
        ("pipeline_latency_p97", 1, "unknown statistic 'p97'"),
        ("pipeline_latency", 1, "unknown statistic 'latency'"),
        ("pipeline_latency_p95", "20 parsecs", "not a limit"),
        ("pipeline_latency_p95", "20", "not a limit"),
        ("pipeline_latency_p95", True, "not a limit"),
        ("pipeline_latency_p95", "20%", "does not take"),
        ("process_cpu_mean", "20ms", "does not take"),
        ("queue_bytes_max", "2ms", "does not take"),
        ("pipeline_latency_n", "2ms", "takes a number"),
    ],
)
def test_a_bad_budget_names_its_keyword(keyword: str, value: Any, message: str) -> None:
    with pytest.raises(ValueError, match=f"^{keyword}: .*{message}"):
        budget.parse({keyword: value})


def test_a_breach_says_what_is_over_what() -> None:
    def stat(p95: float) -> RunStat:
        return RunStat(n=10, mean=p95, p50=p95, p95=p95, p99=p95, max=p95)

    stats = {
        "pipeline.latency": {"a→b": stat(31.2 * MS), "c→d": stat(2 * MS)},
        "queue.fill": {"q": stat(0.95)},
        "process.cpu": {"process": stat(40)},
    }
    budgets = budget.parse(
        {
            "pipeline_latency_p95": "20ms",
            "queue_fill_p95": 0.9,
            "process_cpu_p95": "150%",
            "pad_push_max": "1ms",
        }
    )
    assert budget.check(budgets, stats) == [
        "perf budget: pipeline.latency p95 is 31.2 ms on a→b, over 20 ms",
        "perf budget: queue.fill p95 is 95% on q, over 90%",
        "perf budget: pad.push max was not measured, it has no samples",
    ]


def test_the_text_aligns_test_metric_and_stat_and_never_claims_100_percent() -> None:
    base = index(
        short={"pipeline_latency": (7.05e9, 7.05e9)},
        a_longer_test_id={"element_latency": (3e9, 3e9)},
    )
    new = index(
        short={"pipeline_latency": (29.1 * MS, 29.1 * MS)},
        a_longer_test_id={"element_latency": (0, 0)},
    )
    text = compare.format_text(compare.compare(base, new, "session"))
    rows = [re.match(r"  (\S+) +(\S+) +(p95|max) +", line) for line in text.splitlines()[1:5]]
    assert all(rows)
    starts = [(m.start(2), m.start(3)) for m in rows if m]
    assert len(set(starts)) == 1, text  # metric and stat each start at one column
    assert "-99%" in text and "-100%" in text  # 7.05 s → 29.1 ms is -99%; → 0 is -100%
