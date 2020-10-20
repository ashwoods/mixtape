# type: ignore
import pytest
import asyncio
from mixtape.core import BoomBox

from mixtape import load_mixtape_plugins, hookimpl
from mixtape.core import Command


class ExamplePlugin:
    PIPELINE_STR = "videotestsrc ! fakesink"

    def __init__(self):
        self.state = None

    def setup_plugin_state(self):
        """Initial setup"""
        self.state = "Something not None"

    async def clear(self):
        """Reset calibration on demand"""

    async def call(self):
        """Call calibration on demand"""

    @hookimpl
    def mixtape_setup(self, player, ctx):
        # self.pipeline.get_element
        self.setup_plugin_state()

    @hookimpl
    def mixtape_register_commands(self, player, ctx):
        return [Command("clear", self.clear), Command("call", self.call)]

    @hookimpl
    def mixtape_get_pipeline(self, ctx):
        pipeline_name = ctx.options.get("pipeline")
        if pipeline_name == "test":
            return self.PIPELINE_STR


@pytest.mark.asyncio
async def test_boombox_plugin_hooks(player, pipeline, mocker):
    p = player(pipeline=pipeline)
    pm = load_mixtape_plugins()

    assert not pm.get_plugins(), "Plugins should return empty set"

    plugin = ExamplePlugin()

    pm.register(plugin)

    assert pm.get_plugins(), "We now should have one plugin"

    b = BoomBox(player=p, pm=pm)
    b.setup()

    assert plugin.state == "Something not None"

    await b.play()
    await asyncio.sleep(3)
    await b.call()

    await b.stop()
    await b.clear()
    assert b


def test_init_boombox_with_predefined_pipeline_from_plugin(player):
    pm = load_mixtape_plugins()
    plugin = ExamplePlugin()
    pm.register(plugin)
    b = BoomBox(player=None, pm=pm, pipeline="test")
    assert b
