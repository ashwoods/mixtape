"""What goes wrong: a description that does not parse, a pipeline that fails to start and one
that fails while it plays.

python3 examples/lib_errors.py
"""

# --8<-- [start:description]
import asyncio
import logging

from mixtape import Player
from mixtape.exceptions import PlayerDescriptionError, PlayerPipelineError

logging.getLogger("mixtape").setLevel(logging.CRITICAL)  # the player logs errors too


def typo() -> None:
    try:
        Player.from_description("videotestsrc ! nosuchelement ! fakesink")
    except PlayerDescriptionError as exc:
        print(exc)
        # > Invalid pipeline 'videotestsrc ! nosuchelement ! fakesink': no element "nosuchelement"


# --8<-- [end:description]


# --8<-- [start:start]
async def cannot_start() -> None:
    async with Player.from_description("videotestsrc ! filesink location=/nonexistent/x") as player:
        try:
            await player.play()
        except PlayerPipelineError as exc:
            print(f"{exc.source}: {exc.message}")
            # > filesink0: Could not open file "/nonexistent/x" for writing.
            print("state:", player.state.value_nick)
            # > state: null


# --8<-- [end:start]


# --8<-- [start:playing]
async def fails_while_playing() -> None:
    description = "videotestsrc ! identity name=flaky error-after=5 ! fakesink"
    async with Player.from_description(description) as player:
        try:
            await player.play_until_eos()
        except PlayerPipelineError as exc:
            print(f"{exc.source}: {exc.message}")
            # > flaky: Failed after iterations as requested.
            print("player.error is the exception:", player.error is exc)
            # > player.error is the exception: True

        await player.set("flaky.error-after", -1)  # no more failures
        await player.play()
        print("error after playing again:", player.error)
        # > error after playing again: None


# --8<-- [end:playing]

if __name__ == "__main__":
    typo()
    asyncio.run(cannot_start())
    asyncio.run(fails_while_playing())
