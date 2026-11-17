"""Play, pause and stop a player inside `async with`.

python3 examples/lib_lifecycle.py
"""

# --8<-- [start:lifecycle]
import asyncio

from mixtape import Player


async def main() -> None:
    async with Player.from_description("videotestsrc is-live=true ! fakesink") as player:
        await player.play()
        print(player.state.value_nick)
        await player.pause()
        print(player.state.value_nick)
    print(player.state.value_nick)


asyncio.run(main())
# --8<-- [end:lifecycle]
