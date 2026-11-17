"""The smallest mixtape program: play a pipeline until it ends, and print what it did.

python3 examples/hello.py
"""

# --8<-- [start:main]
import asyncio

from mixtape import Player
from mixtape.events import Eos, StateChanged


async def main() -> None:
    description = "videotestsrc num-buffers=60 ! fakesink"
    async with Player.from_description(description) as player:
        events = player.events()
        await player.play()
        async for event in events:
            if isinstance(event, StateChanged):
                print(f"{event.old.value_nick} -> {event.new.value_nick}")
                # > null -> ready
                # > ready -> paused
                # > paused -> playing
            elif isinstance(event, Eos):
                print("end of stream")
                # > end of stream
                break


asyncio.run(main())
# --8<-- [end:main]
