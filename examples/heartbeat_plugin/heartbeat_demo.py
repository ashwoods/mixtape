"""Enable the heartbeat feature and read its beats.

python3 examples/heartbeat_plugin/heartbeat_demo.py
"""

# --8<-- [start:demo]
import asyncio

from heartbeat import Heartbeat

from mixtape import Player
from mixtape.events import PropertyChanged
from mixtape.exceptions import FeatureError


async def main() -> None:
    description = "videotestsrc is-live=true ! identity ! fakesink"
    async with Player.from_description(description, features=[Heartbeat(interval=0.05)]) as player:
        model = player.describe()
        print("in the model:", *[n for n in (*model.commands, *model.properties) if "." in n])
        # > in the model: heartbeat.reset heartbeat.beats
        events = player.events()
        await player.play()
        async for event in events:
            if isinstance(event, PropertyChanged) and event.value == 3:
                print(f"{event.path} = {event.value}")
                # > heartbeat.beats = 3
                break
        print("reset:", await player.invoke("heartbeat.reset"), player.get("heartbeat.beats"))
        # > reset: None 0
    print("detached:", player.enabled_features["heartbeat"].player is None)
    # > detached: True

    try:
        Heartbeat.from_options(interval="0")
    except FeatureError as exc:
        print(exc)
        # > Invalid options for feature 'heartbeat': interval: Input should be greater than 0
    try:
        Player.from_description("fakesrc", features=[Heartbeat()])
    except FeatureError as exc:
        print(exc)
        # > heartbeat needs a pipeline with a sink


if __name__ == "__main__":
    asyncio.run(main())
# --8<-- [end:demo]
