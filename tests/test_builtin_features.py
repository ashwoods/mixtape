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
from mixtape.features import available
from mixtape.features.leaks import Leaks

STREAMING = (
    "fakesrc name=src sizetype=fixed sizemax=1000 ! identity name=id sleep-time=1000 "
    "! fakesink name=sink"
)


def test_the_builtin_features_are_registered() -> None:
    assert {"leaks", "stats"} <= available().keys()


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
