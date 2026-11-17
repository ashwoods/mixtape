"""Two players in one asyncio loop, linked by intervideosink and intervideosrc: the producer
stops and restarts while the consumer keeps running.

python3 examples/inter_restart.py
"""

# --8<-- [start:players]
import asyncio

from mixtape import Player

# a camera at 10 fps, and a consumer that asks for 30
PRODUCER = (
    "videotestsrc is-live=true ! video/x-raw,width=320,height=240,framerate=10/1"
    " ! intervideosink channel=cam"
)
CONSUMER = "intervideosrc channel=cam ! video/x-raw,framerate=30/1 ! queue ! fakesink name=sink"
# --8<-- [end:players]


# --8<-- [start:count]
def rendered(player: Player) -> int:
    """The frames the consumer's sink has rendered so far."""
    sink = player.pipeline.get_by_name("sink")
    return int(sink.get_property("stats").get_value("rendered"))


async def frames_in_a_second(player: Player) -> int:
    before = rendered(player)
    await asyncio.sleep(1)
    return rendered(player) - before


# --8<-- [end:count]


# --8<-- [start:main]
async def report(step: str, consumer: Player) -> None:
    frames = await frames_in_a_second(consumer)
    print(f"producer {step}: the consumer rendered more than 10 frames: {frames > 10}")
    # > producer playing: the consumer rendered more than 10 frames: True
    # > producer stopped: the consumer rendered more than 10 frames: True
    # > producer restarted: the consumer rendered more than 10 frames: True


async def main() -> None:
    producer = Player.from_description(PRODUCER)
    consumer = Player.from_description(CONSUMER)
    async with producer, consumer:
        await consumer.play()
        await producer.play()
        await report("playing", consumer)
        await producer.stop()
        await report("stopped", consumer)
        await producer.play()
        await report("restarted", consumer)


asyncio.run(main())
# --8<-- [end:main]
