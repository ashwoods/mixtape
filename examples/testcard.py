"""A test card player: a pattern to choose, a frame counter and a command.

python3 examples/testcard.py
"""

# --8<-- [start:player]
from typing import Any, Literal

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player, command, prop

Pattern = Literal["smpte", "ball", "snow"]


class Testcard(Player):
    description = (
        "videotestsrc name=src is-live=true ! video/x-raw,width=320,height=240 ! fakesink name=sink"
    )

    pattern = prop("src.pattern", description="The test pattern")

    def __init__(self, pipeline: Gst.Pipeline, **kwargs: Any) -> None:
        super().__init__(pipeline, **kwargs)
        self._frames = 0
        pad = self.pipeline.get_by_name("sink").get_static_pad("sink")
        pad.add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)

    async def play(self) -> Gst.StateChangeReturn:
        self._frames = 0
        return await super().play()

    @prop
    def frames(self) -> int:
        """Frames shown since play() was called"""
        return self._frames

    @command
    async def show(self, pattern: Pattern = "smpte") -> str:
        """Show a test pattern."""
        await self.set("pattern", pattern)
        return pattern

    def _on_buffer(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        # a streaming thread: count on the loop, which publishes
        self.call_soon_threadsafe(self._count)
        return Gst.PadProbeReturn.OK

    def _count(self) -> None:
        self._frames += 1
        if self._frames % 10 == 0:
            self.notify("frames")


# --8<-- [end:player]


# --8<-- [start:main]
import asyncio

from mixtape.events import PropertyChanged
from mixtape.exceptions import InvalidValueError


async def main() -> None:
    async with Testcard.from_description(Testcard.description) as card:
        spec = card.describe()
        print("commands:", ", ".join(sorted(spec.commands)))
        print("properties:", ", ".join(sorted(spec.properties)))
        print("pattern:", card.get("pattern"))

        events = card.events()
        await card.play()
        await card.set("pattern", "ball")
        print("show:", await card.invoke("show", pattern="snow"))

        # from a thread of your own, run a coroutine on the player's loop
        def from_a_thread() -> str:
            return card.run_coroutine_threadsafe(card.invoke("show", pattern="smpte")).result()

        print("from a thread:", await asyncio.to_thread(from_a_thread))

        try:
            await card.invoke("show", pattern="plaid")
        except InvalidValueError as exc:
            print("rejected:", exc)

        async for event in events:
            if isinstance(event, PropertyChanged) and event.path == "frames":
                print("frames:", event.value)
                break


if __name__ == "__main__":
    asyncio.run(main())
# --8<-- [end:main]
