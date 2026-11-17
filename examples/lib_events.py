"""Read a player's events, and react to one kind of bus message.

python3 examples/lib_events.py
"""

# --8<-- [start:events]
import asyncio

from mixtape import Player
from mixtape.events import Eos, Error, Position, PropertyChanged, StateChanged, Warning


async def main() -> None:
    description = "videotestsrc name=src num-buffers=45 ! fakesink sync=true"  # 1.5 s
    async with Player.from_description(description) as player:
        events = player.events(position=True)
        await player.set("src.pattern", "ball")
        await player.play()
        moving = False
        async for event in events:
            match event:
                case StateChanged(new=state) if state.value_nick == "playing":
                    print("playing")
                case PropertyChanged(path=path, value=value):
                    print(f"{path} = {value}")
                case Position(position=position) if position and not moving:
                    print("the position moves")
                    moving = True
                case Warning(message=message):
                    print("warning:", message)
                case Error(error=error):
                    print("error:", error.message)
                    break
                case Eos():
                    print("end of stream")
                    break


asyncio.run(main())
# --8<-- [end:events]
