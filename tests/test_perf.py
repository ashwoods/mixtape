import io
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from mixtape import Player
from mixtape.exceptions import FeatureError
from mixtape.perf import RUST, Artifacts, Sample, Totals, TracerLog, percentile, rates, window
from mixtape.perf.reader import LogTail

FIXTURES = Path(__file__).parent / "fixtures" / "perf"
"""Recorded from `videotestsrc num-buffers=3 ! video/x-raw,width=64,height=48 ! queue name=q
! queue name=u max-size-buffers=0 max-size-bytes=0 max-size-time=0 ! fakesink sync=true`
with `latency(flags=element+pipeline);stats;queue-levels;buffer-lateness;pad-push-timings`;
the debug log (`gst-debug.txt`, as `*.log` is ignored) keeps two records of each kind it ignores"""
PADS = ("videotestsrc0:src", "capsfilter0:src", "q:src", "u:src")
FRAME = 64 * 48 * 8  # the first format videotestsrc offers, 16 bits a channel


def rust_tracers(Gst: Any) -> bool:
    return isinstance(Gst.Registry.get().lookup_feature("queue-levels"), Gst.TracerFactory)


# -- parsers -- #


def test_the_tracer_log_gives_latencies_and_buffers_per_pad() -> None:
    parser = TracerLog()
    with open(FIXTURES / "gst-debug.txt") as log:
        assert all(map(parser.line, log))  # every line is the tracers' own
    assert not parser.line("0:00:00.1 1 0x1 WARN videotestsrc gstvideotestsrc.c:1:f: hello")
    assert parser.elements == {
        0: "videotestsrc0",
        1: "q",
        2: "u",
        3: "fakesink0",
        4: "pipeline0",
        5: "capsfilter0",
    }
    samples, counts = parser.take()
    by: dict[str, dict[str, list[float]]] = {}
    for sample in samples:
        by.setdefault(sample.metric, {}).setdefault(sample.subject, []).append(sample.value)
    assert {metric: set(subjects) for metric, subjects in by.items()} == {
        "element.latency": {"capsfilter0", "q", "u"},
        "pipeline.latency": {"videotestsrc0→fakesink0"},
        "pad.interval": set(PADS),
    }
    assert by["element.latency"]["q"] == [77083, 121208, 178292]
    assert by["pipeline.latency"]["videotestsrc0→fakesink0"] == [1091959, 1211084, 104217084]
    assert all(len(values) == 2 for values in by["pad.interval"].values())
    assert counts == dict.fromkeys(PADS, (3, 3 * FRAME))
    assert parser.take() == ([], dict.fromkeys(PADS, (0, 0)))  # the pads stay, the counts not


def test_the_tracer_log_keeps_the_names_but_not_the_samples_when_not_collecting() -> None:
    parser = TracerLog()
    parser.collecting = False
    with open(FIXTURES / "gst-debug.txt") as log:
        for line in log:
            parser.line(line)
    assert parser.take() == ([], {}) and len(parser.pads) == 8


def test_the_rust_csvs_give_queue_levels_lateness_and_push_times() -> None:
    def parse(name: str) -> list[Sample]:
        return RUST[name]((FIXTURES / f"{name}.csv").read_text().splitlines())

    levels = parse("queue-levels")
    assert {s.metric for s in levels} == {
        "queue.bytes",
        "queue.buffers",
        "queue.time",
        "queue.fill",
    }
    fill = [s for s in levels if s.metric == "queue.fill"]
    assert {s.subject for s in fill} == {"q"}  # `u` has no limits
    assert [s.value for s in fill][:2] == [33333333 / 1e9, 0]  # time is the fullest limit
    u = [s.value for s in levels if s.metric == "queue.buffers" and s.subject == "u"]
    assert u == [1, 0, 1, 2, 1, 0]
    assert parse("buffer-lateness") == [
        Sample(102592917, "pad.lateness", "u:src", -32637624),
        Sample(205883292, "pad.lateness", "u:src", 37125543),
    ]
    push = parse("pad-push-timings")
    assert len(push) == 12 and {s.subject for s in push} == set(PADS)
    assert max(push, key=lambda s: s.value) == Sample(102592917, "pad.push", "u:src", 103163458)


def test_the_rust_csvs_keep_commas_in_names_and_skip_what_is_not_a_row() -> None:
    lines = ["1,a,b:src,0x1,5", "", "garbage", "2,c:src,0x2,x"]
    assert RUST["pad-push-timings"](lines) == [Sample(1, "pad.push", "a,b:src", 5)]


# -- aggregation -- #


def test_percentiles_interpolate_between_ranks() -> None:
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([5], 0.95) == 5
    assert percentile([0, 10], 0.95) == pytest.approx(9.5)


def test_a_window_has_the_statistics_of_each_metric_and_subject() -> None:
    samples = [
        Sample(3, "element.latency", "q", 30),
        Sample(1, "element.latency", "q", 10),
        Sample(2, "element.latency", "q", 20),
        Sample(2, "element.latency", "x", 7),
    ]
    result = window(samples, 0, 10)
    q = result.metrics["element.latency"]["q"]
    assert (q.n, q.mean, q.p50, q.max, q.last) == (3, 20, 20, 30, 30)  # last by time
    assert q.p95 == pytest.approx(29)
    assert set(result.metrics["element.latency"]) == {"q", "x"}


def test_rates_are_per_second_over_the_window() -> None:
    samples = rates({"q:src": (15, 1000)}, 0, 500_000_000)
    assert samples == [
        Sample(500_000_000, "pad.buffers", "q:src", 30),
        Sample(500_000_000, "pad.bitrate", "q:src", 16000),
    ]
    assert rates({"q:src": (1, 1)}, 5, 5) == []


def test_totals_have_the_statistics_of_the_whole_run() -> None:
    totals = Totals()
    totals.add(Sample(i, "pad.push", "q:src", i) for i in range(1, 101))
    stat = totals.stats()["pad.push"]["q:src"]
    assert (stat.n, stat.mean, stat.p50, stat.max) == (100, 50.5, 50.5, 100)
    assert stat.p99 == pytest.approx(99.01)


def test_the_artifacts_have_every_sample_window_and_the_run(tmp_path: Path) -> None:
    (tmp_path / "pad.push.csv").write_text("stale")
    (tmp_path / "notes.csv").write_text("kept")
    artifacts = Artifacts(tmp_path)
    first = [Sample(1, "element.latency", "q", 10), Sample(2, "queue.fill", "q", 0.5)]
    artifacts.add(first, window(first, 0, 5))
    second = [Sample(6, "element.latency", "q", 30)]
    artifacts.add(second, window(second, 5, 10))
    artifacts.finish({"pipeline": "p"})
    assert not (tmp_path / "pad.push.csv").exists() and (tmp_path / "notes.csv").exists()
    lines = (tmp_path / "samples.jsonl").read_text().splitlines()
    assert json.loads(lines[0]) == {
        "t_ns": 1,
        "metric": "element.latency",
        "subject": "q",
        "value": 10,
    }
    assert len(lines) == 3
    assert (tmp_path / "element.latency.csv").read_text().splitlines() == [
        "t_ns,subject,value",
        "1,q,10",
        "6,q,30",
    ]
    windows = [json.loads(w) for w in (tmp_path / "windows.jsonl").read_text().splitlines()]
    assert [(w["start"], w["end"]) for w in windows] == [(0, 5), (5, 10)]
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["pipeline"] == "p" and summary["windows"] == 2
    latency = summary["metrics"]["element.latency"]
    assert latency["unit"] == "ns" and latency["subjects"]["q"]["mean"] == 20


# -- the log reader -- #


def test_the_reader_parses_the_tracer_lines_and_echoes_the_others(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    lines = (FIXTURES / "gst-debug.txt").read_text().splitlines(keepends=True)
    other = "0:00:00.1 1 0x1 WARN videotestsrc gstvideotestsrc.c:1:f: hello\n"
    path.write_text("".join(lines[:40]) + other + lines[40][:30])  # GStreamer is mid-line
    echo = io.StringIO()
    tail = LogTail(path, echo)
    try:
        tail.collect(True)
        with open(path, "a") as log:
            log.write(lines[40][30:] + "".join(lines[41:]))
        samples, counts = tail.take()
    finally:
        tail.close()
    assert echo.getvalue() == other
    assert tail.lines == len(lines) + 1
    assert counts == dict.fromkeys(PADS, (3, 3 * FRAME))
    assert len([s for s in samples if s.metric == "element.latency"]) == 9


# -- the feature -- #


def test_perf_needs_the_tracers_from_before_gstreamer_initialised(
    Gst: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if any(t.__gtype__.name == "GstLatencyTracer" for t in Gst.tracing_get_active_tracers()):
        pytest.skip("the tracer is active in this process")
    for key in ("GST_TRACERS", "GST_DEBUG", "GST_DEBUG_FILE", "GST_DEBUG_NO_COLOR"):
        monkeypatch.setenv(key, os.environ.get(key, ""))  # `prepare` sets them: restored
    with pytest.raises(FeatureError, match="'latency', 'stats', but .* features=\\['perf'\\]"):
        Player.from_description("fakesrc ! fakesink", features=["perf"])


def run_env() -> dict[str, str]:
    unset = ("GST_TRACERS", "GST_DEBUG", "GST_DEBUG_FILE")
    return {key: value for key, value in os.environ.items() if key not in unset}


def test_mixtape_run_writes_the_artifacts(tmp_path: Path, Gst: Any) -> None:
    out = tmp_path / "out"
    pipeline = "videotestsrc num-buffers=30 ! video/x-raw,width=64,height=48 ! queue ! fakesink"
    done = subprocess.run(
        [sys.executable, "-m", "mixtape", "run", pipeline, "--feature", f"perf,out={out}"],
        capture_output=True,
        text=True,
        timeout=30,
        env=run_env(),
    )
    assert done.returncode == 0, done.stderr
    assert "GST_TRACER" not in done.stderr  # its log is read, not shown
    for name in ("samples.jsonl", "windows.jsonl", "element.latency.csv", "pad.buffers.csv"):
        assert (out / name).stat().st_size > 0, name
    summary = json.loads((out / "summary.json").read_text())
    assert summary["pipeline"] == pipeline and summary["windows"] >= 1
    metrics = summary["metrics"]
    assert metrics["element.latency"]["subjects"]["queue0"]["n"] == 30
    assert metrics["pipeline.latency"]["subjects"]["videotestsrc0→fakesink0"]["n"] == 30
    assert metrics["process.cpu"]["unit"] == "%"
    if rust_tracers(Gst):
        assert summary["tracers"][2:] == ["queue-levels", "buffer-lateness", "pad-push-timings"]
        assert metrics["queue.fill"]["subjects"]["queue0"]["max"] > 0
    else:
        assert summary["tracers"] == ["latency(flags=element+pipeline)", "stats"]
    assert {(link["source"], link["sink"]) for link in summary["graph"]["links"]} == {
        ("videotestsrc0", "capsfilter0"),
        ("capsfilter0", "queue0"),
        ("queue0", "fakesink0"),
    }
    # the report: one page with all it needs, which fetches nothing
    report = (out / "report.html").read_text()
    assert "uPlot" in report and "function renderGraph(" in report
    assert '"element.latency"' in report and "queue0" in report  # the run, inlined
    assert "default-src 'none'" in report
    assert not re.search(r"https?://|\b(src|href)\s*=|@import|url\(", report)


SUMMARY_SCRIPT = textwrap.dedent("""
    import asyncio, json
    from mixtape import Player

    async def main():
        player = Player.from_description(
            "videotestsrc is-live=true ! video/x-raw,width=64,height=48 ! queue name=q "
            "! fakesink name=sink",
            features=["perf,interval=0.2"],
        )
        assert player.get("perf.summary") is None
        async with player:
            events = player.events()
            await player.play()
            async for event in events:
                if getattr(event, "path", None) == "perf.summary" and event.value["metrics"].get(
                    "element.latency"
                ):
                    break
            published = event.value
        print(json.dumps({"published": published, "after": player.get("perf.summary")}))

    asyncio.run(main())
""")


def test_perf_publishes_the_summary_while_running() -> None:
    """In a process of its own: tracers exist only when GStreamer initialises with them."""
    done = subprocess.run(
        [sys.executable, "-c", SUMMARY_SCRIPT],
        capture_output=True,
        text=True,
        timeout=30,
        env=run_env(),
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.splitlines()[-1])
    published = result["published"]
    assert 0 < published["end"] - published["start"] < 1e9
    metrics = published["metrics"]
    assert metrics["element.latency"]["q"]["n"] >= 1
    assert metrics["pad.buffers"]["q:src"]["last"] > 0  # the queue pushed: per second
    assert "process" in metrics["process.cpu"]
    assert result["after"] is None
