"""The `gst:` features (any GStreamer tracer) and the action signals of exposed elements."""

import json
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest

from mixtape import Player
from mixtape.exceptions import FeatureError
from mixtape.features import resolve
from mixtape.features.gst import GstTracer
from mixtape.features.perf import Perf
from mixtape.features.tracers import installed
from mixtape.model import _parse_structure, to_json


def test_structures_become_json(Gst: Any) -> None:
    structure = _parse_structure(
        "s, n=(int)1, u=(guint64)18446744073709551615, f=(fraction)30/1, b=(boolean)true, "
        'l={ (int)1, (int)2 }, a=<(string)a>, c=(GstCaps)"video/x-raw", '
        'nested=(structure)"t\\,\\ x\\=\\(int\\)3\\;", '
        'objects={ (structure)"o\\,\\ type-name\\=\\(string\\)GstPad\\;" }'
    )
    assert to_json(structure) == {
        "n": 1,
        "u": 2**64 - 1,
        "f": "30/1",
        "b": True,
        "l": [1, 2],
        "a": ["a"],
        "c": "video/x-raw",
        "nested": {"x": 3},
        "objects": [{"type-name": "GstPad"}],  # as in a checkpoint of the leaks tracer
    }


@pytest.mark.asyncio
async def test_action_signals_of_exposed_elements_are_commands() -> None:
    player = Player.from_description("appsrc name=src ! fakesink splitmuxsink name=mux")
    commands = player.describe().commands
    eos = commands["src.end-of-stream"]
    assert "ok" in eos.result["enum"]  # type: ignore[index]
    split = commands["mux.split-at-running-time"]
    assert split.parameters["properties"]["arg1"]["minimum"] == 0
    assert split.parameters["properties"]["arg1"]["description"] == "guint64"
    assert not any(name.startswith("fakesink") for name in commands)  # not exposed
    async with player:
        assert await player.invoke("src.end-of-stream") == "ok"
        assert await player.invoke("mux.split-at-running-time", arg1=5) is None


def test_gst_options_are_the_tracer_parameters() -> None:
    feature = resolve("gst:latency,flags=element+pipeline,filter=(a,b)")
    assert isinstance(feature, GstTracer)
    assert feature.name == "gst:latency"
    assert feature.entry == "latency(flags=element+pipeline,filter=(a,b))"
    assert resolve("gst:rusage").entry == "rusage"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("spec", "message"),
    [("gst:leaks", "use the feature 'leaks'"), ("gst:a;b", "Invalid tracer name")],
)
def test_gst_refuses(spec: str, message: str) -> None:
    with pytest.raises(FeatureError, match=message):
        resolve(spec)


def test_gst_refuses_a_tracer_another_feature_reads() -> None:
    player: Any = SimpleNamespace(enabled_features={"perf": Perf()})
    with pytest.raises(FeatureError, match="cannot run beside the feature 'perf'"):
        resolve("gst:queue-levels").check(player)


GST_SCRIPT = textwrap.dedent("""
    import asyncio, json, os, sys
    from mixtape import Player

    async def main():
        player = Player.from_description(
            "videotestsrc num-buffers=30 ! queue name=q ! fakesink",
            features=["gst:latency,flags=element", *sys.argv[2:]],
        )
        spec = player.describe()
        result = {
            "commands": [n for n in spec.commands if n.startswith("gst:")],
            "properties": [p for p in spec.properties if p.startswith("gst:")],
            "flags": player.get("gst:latency.flags"),
        }
        async with player:
            await player.play_until_eos()
            if "gst:queue-levels" in sys.argv:
                await player.invoke("gst:queue-levels.write-log", arg1=sys.argv[1])
        print(json.dumps(result))

    asyncio.run(main())
""")


def test_gst_tracers_become_features(Gst: Any, tmp_path: Any) -> None:
    """In a process of its own: tracers exist only when GStreamer initialises with them."""
    rust = ["gst:queue-levels"] if installed(["queue-levels"]) else []
    log = tmp_path / "queue-levels.csv"
    env = {key: value for key, value in os.environ.items() if key != "GST_TRACERS"}
    done = subprocess.run(
        [sys.executable, "-c", GST_SCRIPT, str(log), *rust],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.splitlines()[-1])
    assert {"gst:latency.flags", "gst:latency.params"} <= set(result["properties"])
    assert result["flags"] == ["element"]
    if rust:
        assert result["commands"] == ["gst:queue-levels.write-log"]
        assert ",q," in log.read_text()  # a CSV line of the queue's levels
