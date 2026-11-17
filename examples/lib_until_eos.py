"""Play until the stream ends, and again.

python3 examples/lib_until_eos.py
"""

# --8<-- [start:until_eos]
import asyncio

from mixtape import Player


async def main() -> None:
    async with Player.from_description("videotestsrc num-buffers=100 ! fakesink") as player:
        for run in (1, 2):
            await player.play_until_eos()
            print(f"run {run}: {player.state.value_nick} at the end of the stream")


asyncio.run(main())
# --8<-- [end:until_eos]
