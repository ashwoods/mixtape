"""Watch the pipeline change shape: a branch added to a tee while it plays.

python3 examples/lib_shape.py
"""

# --8<-- [start:shape]
import asyncio

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player
from mixtape.events import GraphChanged, ModelChanged

DESCRIPTION = "videotestsrc is-live=true ! tee name=t ! queue ! fakesink name=main"


async def main() -> None:
    async with Player.from_description(DESCRIPTION) as player:
        events = player.events()
        await player.play()
        print("exposed:", ", ".join(player.describe().elements))
        # > exposed: t, main

        # a second branch: this changes the graph, and the model with it, as `extra` is named
        branch = Gst.parse_bin_from_description("queue ! fakesink name=extra", True)
        player.pipeline.add(branch)
        player.pipeline.get_by_name("t").link(branch)
        branch.sync_state_with_parent()

        async for event in events:
            if isinstance(event, GraphChanged) and "extra" in event.graph.nodes:
                print("links:", len(event.graph.links))
                # > links: 5
            elif isinstance(event, ModelChanged):
                print("exposed:", ", ".join(event.model.elements))
                # > exposed: t, main, extra
                break


asyncio.run(main())
# --8<-- [end:shape]
