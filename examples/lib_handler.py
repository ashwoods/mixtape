"""React to one kind of bus message with `add_handler`.

python3 examples/lib_handler.py
"""

# --8<-- [start:handler]
import asyncio

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player


async def main() -> None:
    async with Player.from_description("videotestsrc num-buffers=10 ! fakesink") as player:
        remove = player.add_handler(
            Gst.MessageType.STREAM_START, lambda message: print("a stream started")
        )
        await player.play_until_eos()
        remove()  # a handler stays until it is removed
        await player.play_until_eos()  # no second line: it was removed
        print("done")


asyncio.run(main())
# --8<-- [end:handler]
