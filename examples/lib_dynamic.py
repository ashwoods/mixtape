"""A player for a pipeline built in code.

python3 examples/lib_dynamic.py
"""

# --8<-- [start:build]
import asyncio

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player


def build() -> Gst.Pipeline:
    Gst.init(None)
    pipeline = Gst.Pipeline.new("dynamic")
    src = Gst.ElementFactory.make("videotestsrc", "src")
    src.set_property("num-buffers", 30)
    sink = Gst.ElementFactory.make("fakesink", "sink")
    pipeline.add(src)
    pipeline.add(sink)
    src.link(sink)
    return pipeline


async def main() -> None:
    async with Player(build()) as player:
        print("elements:", ", ".join(player.describe().elements))
        # > elements: dynamic, src, sink
        print("links:", [(link.source, link.sink) for link in player.graph().links])
        # > links: [('src', 'sink')]
        await player.play_until_eos()
        print("played")
        # > played


asyncio.run(main())
# --8<-- [end:build]
