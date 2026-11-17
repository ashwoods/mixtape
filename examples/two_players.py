"""Two players in one process: each is served on its own port, and each measures with `perf`
what both pipelines do.

python3 examples/two_players.py
"""

# --8<-- [start:serve]
import asyncio
import json
import urllib.request

from mixtape import Player
from mixtape.interfaces.http import serve

CAM = "videotestsrc is-live=true name=cam ! queue name=q ! fakesink"
MIC = "audiotestsrc is-live=true name=mic ! queue name=q ! fakesink"


def get_json(url: str) -> dict[str, dict[str, object]]:
    with urllib.request.urlopen(url) as response:
        return json.load(response)


async def page_elements(url: str) -> list[str]:
    """The elements a control page of a player lists."""
    model = await asyncio.to_thread(get_json, f"{url}/api/model")
    return sorted(model["elements"])


# --8<-- [end:serve]


# --8<-- [start:perf]
def pad_subjects(player: Player) -> list[str]:
    """The pads `perf` of a player has counted buffers of."""
    summary = player.get("perf.summary")
    return sorted(summary["metrics"]["pad.buffers"]) if summary else []


async def both_measured(*players: Player) -> None:
    while not all(len(pad_subjects(player)) >= 3 for player in players):
        await asyncio.sleep(0.1)


# --8<-- [end:perf]


# --8<-- [start:main]
async def main() -> None:
    cam = Player.from_description(CAM, features=["perf"])
    mic = Player.from_description(MIC, features=["perf"])
    async with (
        cam,
        mic,
        serve(cam, "127.0.0.1", 0) as cam_page,
        serve(mic, "127.0.0.1", 0) as mic_page,
    ):
        await asyncio.gather(cam.play(), mic.play())
        print("two ports:", cam_page.port != mic_page.port)
        print("cam's page lists:", await page_elements(cam_page.url))
        print("mic's page lists:", await page_elements(mic_page.url))
        await asyncio.wait_for(both_measured(cam, mic), 15)
        print("cam's perf counted buffers of:", pad_subjects(cam))
        print("mic's perf counted buffers of:", pad_subjects(mic))


asyncio.run(main())
# --8<-- [end:main]
