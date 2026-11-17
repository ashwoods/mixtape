"""Serve a player over HTTP from your own program, and read and set it like any client.

python3 examples/http_serve.py
"""

# --8<-- [start:serve]
import asyncio
import json
import urllib.request

from mixtape import Player
from mixtape.interfaces.http import serve


def patch(url: str, values: dict[str, float]) -> dict[str, float]:
    request = urllib.request.Request(url, json.dumps(values).encode(), method="PATCH")
    with urllib.request.urlopen(request) as response:
        return json.load(response)


async def main() -> None:
    player = Player.from_description("audiotestsrc is-live=true ! volume name=vol ! fakesink")
    # port 0 lets the system pick a free one: `listening` says which
    async with player, serve(player, "127.0.0.1", 0) as listening:
        await player.play()
        print("serving a player named", player.describe().name)
        # > serving a player named Player
        # the server runs in this loop: a client in the same program must not block it
        reply = await asyncio.to_thread(patch, f"{listening.url}/api/elements/vol", {"volume": 0.5})
        print("the server says", reply)
        # > the server says {'volume': 0.5}
        print("the player says", player.get("vol.volume"))
        # > the player says 0.5


asyncio.run(main())
# --8<-- [end:serve]
