"""The examples of docs/cli.md."""

import importlib
from pathlib import Path

import pytest

from tests.examples import EXAMPLES


@pytest.mark.asyncio
@pytest.mark.covers("demo:player")
async def test_demo_has_an_alias_and_a_command(
    Gst: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(Path(EXAMPLES)))
    demo = importlib.import_module("demo")
    assert demo.Demo.description is not None
    player = demo.Demo.from_description(demo.Demo.description)
    async with player:
        spec = player.describe()
        assert spec.properties["gain"].target == "vol.volume"
        await player.set("gain", "0.5")  # "0.5" is validated and converted
        assert player.get("vol.volume") == 0.5
        assert await player.invoke("shout", word="hi") == "hihi"
        assert await player.invoke("shout", word="a", times=3) == "aaa"
