"""A command that says when it can run.

python3 examples/lib_available.py
mixtape run --http 8047 examples/lib_available.py:Recorder
"""

# --8<-- [start:available]
import asyncio

from mixtape import Player, command
from mixtape.events import CommandsChanged
from mixtape.exceptions import CommandError


class Recorder(Player):
    description = "audiotestsrc is-live=true ! fakesink"
    recording = False

    @command
    def start(self) -> None:
        self.recording = True

    @command(when=lambda self: self.recording or "Not recording")
    def finish(self) -> None:
        self.recording = False


async def main() -> None:
    async with Recorder.from_description() as recorder:
        await recorder.play()
        events = recorder.events()
        print(recorder.unavailable())
        # > {'finish': 'Not recording'}
        try:
            await recorder.invoke("finish")
        except CommandError as exc:
            print(exc)
            # > Not recording
        await recorder.invoke("start")
        async for event in events:
            if isinstance(event, CommandsChanged):
                print(event.unavailable)
                # > {}
                break


if __name__ == "__main__":
    asyncio.run(main())
# --8<-- [end:available]
