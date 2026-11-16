import asyncio
import json
import os
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from mixtape import Player
from mixtape.events import EventStream, PropertyChanged
from mixtape.exceptions import FeatureError
from mixtape.features import available, resolve
from mixtape.features.leaks import Leaks
from mixtape.features.tracers import Tracers

STREAMING = (
    "fakesrc name=src sizetype=fixed sizemax=1000 ! identity name=id sleep-time=1000 "
    "! fakesink name=sink"
)


def test_the_builtin_features_are_registered() -> None:
    assert {"leaks", "perf", "stats", "tracers"} <= available().keys()


# -- stats -- #


async def flowing_rates(stream: EventStream) -> Any:
    """The first published `stats.rates` in which `src` has buffers."""
    async for event in stream:
        if isinstance(event, PropertyChanged) and event.path == "stats.rates":
            if event.value.get("src", {}).get("buffers_per_s", 0) > 0:
                return event.value
    raise AssertionError("the event stream ended")


@pytest.mark.asyncio
async def test_stats_publishes_the_rates_of_the_exposed_elements(Gst: Any) -> None:
    player = Player.from_description(STREAMING, features=["stats,interval=0.1"])
    description = player.describe().properties["stats.rates"].description
    assert description is not None and description.startswith("Buffers and kilobits")
    assert player.get("stats.rates") == {}  # not attached
    for _ in range(2):  # and again after a teardown
        async with player:
            events = player.events()
            await player.play()
            published = await asyncio.wait_for(flowing_rates(events), 5)
            assert set(published) == {"src", "id"}  # the sink has no source pad
            assert set(published["src"]) == {"buffers_per_s", "kbit_per_s"}
            assert published["src"]["kbit_per_s"] > 0
            assert player.get("stats.rates").keys() == published.keys()
        assert player.get("stats.rates") == {}


# -- leaks -- #

LEAKS_SCRIPT = textwrap.dedent("""
    import asyncio, json
    from mixtape import Player

    async def main():
        player = Player.from_description(
            "fakesrc sizetype=fixed sizemax=100 ! identity sleep-time=1000 ! fakesink",
            features=["leaks,interval=0.1"],
        )
        assert player.get("leaks.alive") == {}
        async with player:
            events = player.events()
            await player.play()
            async for event in events:
                if getattr(event, "path", None) == "leaks.alive" and event.value:
                    break
            alive = player.get("leaks.alive")
            first = await player.invoke("leaks.checkpoint")
            await asyncio.sleep(0.3)
            second = await player.invoke("leaks.checkpoint")
            await player.invoke("leaks.reset")
            reset = player.get("leaks.alive")
        print(json.dumps({"alive": alive, "first": first, "second": second, "reset": reset,
                          "after": player.get("leaks.alive")}))

    asyncio.run(main())
""")


def test_leaks_counts_live_objects_by_type() -> None:
    """In a process of its own: the tracer exists only when GStreamer initialises with it."""
    env = {key: value for key, value in os.environ.items() if key != "GST_TRACERS"}
    done = subprocess.run(
        [sys.executable, "-c", LEAKS_SCRIPT], capture_output=True, text=True, timeout=15, env=env
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.splitlines()[-1])
    for counts in (result["alive"], result["first"], result["second"]):
        assert all(isinstance(k, str) and isinstance(v, int) for k, v in counts.items())
    assert result["alive"] and result["first"]
    assert list(result["alive"].values()) == sorted(result["alive"].values(), reverse=True)
    assert result["reset"] == {} == result["after"]


LEAKS_WARNING_SCRIPT = textwrap.dedent("""
    import asyncio, json, logging
    from mixtape import Player
    from gi.repository import Gst

    records = []
    class Collect(logging.Handler):
        def emit(self, record):
            records.append((record.name, record.levelname, record.getMessage()))
    logging.getLogger("mixtape").addHandler(Collect())

    async def main():
        player = Player.from_description(
            "videotestsrc num-buffers=10 ! fakesink", features=["leaks,interval=0.1"]
        )
        async with player:
            await player.play_until_eos()
            KEEP
    asyncio.run(main())
    print(json.dumps(records))
""")


def _leaks_warnings(keep: str) -> list[str]:
    """The warnings of the leaks feature at the end of a run that executes `keep` in it."""
    env = {key: value for key, value in os.environ.items() if key != "GST_TRACERS"}
    script = LEAKS_WARNING_SCRIPT.replace("KEEP", keep)
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15, env=env
    )
    assert done.returncode == 0, done.stderr
    records = json.loads(done.stdout.splitlines()[-1])
    return [msg for name, level, msg in records if name == "mixtape.features.leaks"]


def test_leaks_a_clean_run_logs_no_warning() -> None:
    assert _leaks_warnings("pass") == []


def test_leaks_logs_what_is_alive_after_the_run() -> None:
    # `held` outlives the teardown (a module global): the tracer sees a buffer nobody freed
    warnings = _leaks_warnings("global held; held = Gst.Buffer.new_allocate(None, 16, None)")
    assert len(warnings) == 1, warnings
    assert warnings[0].startswith("Still alive after the run: ")
    assert "1 GstBuffer" in warnings[0]
    assert "GstSystemClock" not in warnings[0]
    assert "GstTaskPool" not in warnings[0]


def test_leaks_needs_the_tracer_from_before_gstreamer_initialised(
    Gst: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if any(t.__gtype__.name == "GstLeaksTracer" for t in Gst.tracing_get_active_tracers()):
        pytest.skip("the tracer is active in this process")
    monkeypatch.setenv("GST_TRACERS", "")  # `prepare` sets it: restored afterwards
    with pytest.raises(FeatureError, match="initialised before it could set GST_TRACERS") as caught:
        Player.from_description("fakesrc ! fakesink", features=["leaks"])
    assert "--feature leaks" in str(caught.value)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (None, "leaks"),
        ("latency", "latency;leaks"),
        ("leaks(stack-traces-flags=none);latency", "leaks(stack-traces-flags=none);latency"),
    ],
)
def test_leaks_adds_itself_to_the_tracers(
    before: str | None, after: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GST_TRACERS", before or "")
    Leaks().prepare()
    assert os.environ["GST_TRACERS"] == after


# -- tracers -- #


def test_tracers_options_keep_the_commas_of_their_parameters() -> None:
    feature = resolve("tracers,names=latency(flags=element,filter=x);rusage,log=false")
    assert isinstance(feature, Tracers)
    assert feature.names == ["latency(flags=element,filter=x)", "rusage"]
    assert feature.log is False


@pytest.mark.parametrize(
    ("spec", "env", "after", "warned"),
    [
        (
            "names=latency;queue-levels",
            {},
            {"GST_TRACERS": "latency;queue-levels", "GST_DEBUG": "GST_TRACER:7"},
            [],
        ),
        (
            "names=queue-levels",
            {"GST_DEBUG": "2"},
            {"GST_TRACERS": "queue-levels", "GST_DEBUG": "2"},
            [],
        ),
        (
            "names=rusage",
            {"GST_TRACERS": "leaks", "GST_DEBUG": "3"},
            {"GST_TRACERS": "leaks;rusage", "GST_DEBUG": "3,GST_TRACER:7"},
            [],
        ),
        (
            "names=latency(flags=element);stats",
            {"GST_TRACERS": "latency", "GST_DEBUG": "GST_TRACER:5"},
            {"GST_TRACERS": "latency;stats", "GST_DEBUG": "GST_TRACER:5"},
            ["'latency': keeping it rather than 'latency(flags=element)'", "'GST_TRACER:5'"],
        ),
    ],
)
def test_tracers_add_to_the_environment_and_keep_what_it_has(
    spec: str,
    env: dict[str, str],
    after: dict[str, str],
    warned: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    keys = ("GST_TRACERS", "GST_DEBUG")
    for key in keys:
        monkeypatch.delenv(key, raising=False)  # and restored afterwards
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    resolve(f"tracers,{spec}").prepare()
    assert {key: os.environ[key] for key in keys if key in os.environ} == after
    messages = [r.getMessage() for r in caplog.records if r.name == "mixtape.features.tracers"]
    assert len(messages) == len(warned)
    assert all(text in message for text, message in zip(warned, messages, strict=True))


TRACERS_SCRIPT = textwrap.dedent("""
    import asyncio, json
    from mixtape import Player

    async def main():
        player = Player.from_description(
            "videotestsrc num-buffers=5 ! videoconvert ! fakesink",
            features=["leaks", "tracers,names=latency(flags=element);rusage"],
        )
        async with player:
            await player.play_until_eos()
        print(json.dumps(player.get("tracers.active")))

    asyncio.run(main())
""")


def test_tracers_run_the_tracers_they_name() -> None:
    """In a process of its own: tracers exist only when GStreamer initialises with them."""
    unset = ("GST_TRACERS", "GST_DEBUG")
    env = {key: value for key, value in os.environ.items() if key not in unset}
    done = subprocess.run(
        [sys.executable, "-c", TRACERS_SCRIPT], capture_output=True, text=True, timeout=15, env=env
    )
    assert done.returncode == 0, done.stderr
    active = json.loads(done.stdout.splitlines()[-1])
    assert {"latency", "leaks", "rusage"} <= set(active)
    assert "element-latency, element-id=" in done.stderr  # the log the core tracers write to


@pytest.mark.parametrize(
    ("names", "message"),
    [
        ("nosuchtracer;videotestsrc", "'nosuchtracer', 'videotestsrc', which GStreamer has no"),
        ("rusage", "'rusage', but .* was initialised before it could set GST_TRACERS"),
    ],
)
def test_tracers_need_installed_tracers_from_before_gstreamer_initialised(
    names: str, message: str, Gst: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if any(t.__gtype__.name == "GstRUsageTracer" for t in Gst.tracing_get_active_tracers()):
        pytest.skip("the tracer is active in this process")
    for key in ("GST_TRACERS", "GST_DEBUG"):
        monkeypatch.setenv(key, os.environ.get(key, ""))  # `prepare` sets them: restored
    with pytest.raises(FeatureError, match=message):
        Player.from_description("fakesrc ! fakesink", features=[f"tracers,names={names}"])
