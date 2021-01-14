# type: ignore
import pytest
import asyncio
from mixtape import BoomBox, hookspec


@pytest.mark.asyncio
async def test_boombox_usage(Gst):
    class PluginA:
        @hookspec.impl
        def mixtape_add_pipelines(self, ctx):
            return {"simple": "videotestsrc ! queue ! fakesink"}

    class PluginB:
        @hookspec.impl
        def mixtape_add_pipelines(self, ctx):
            return {"error": "filesink ! queue ! fakesink"}

    b = await BoomBox.init(plugins=[PluginA, PluginB], name="simple")
    await b.play()
    await asyncio.sleep(5)
    await b.stop()
