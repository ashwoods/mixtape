"""A pipeline that changes shape while it plays, and the WebSocket messages that say so.

python3 examples/http_shape.py
"""

import asyncio
import json
import tempfile
from pathlib import Path

import websockets

from mixtape import Player
from mixtape.interfaces.http import serve


# --8<-- [start:watch]
async def watch(url: str) -> None:
    graphs = models = 0
    async with websockets.connect(f"{url}/api/events") as ws:
        for _ in range(3):  # the model, the values and the graph the connection opens with
            message = json.loads(await ws.recv())
            if message["type"] == "graph":
                nodes = message["graph"]["nodes"]
                print("at first, wavparse0 is in the graph:", "wavparse0" in nodes)
        await ws.send(json.dumps({"id": 1, "command": "play"}))
        # decodebin adds a wavparse while it prerolls: the graph and the model follow
        while not (graphs and models):
            message = json.loads(await ws.recv())
            if message["type"] == "graph" and "wavparse0" in message["graph"]["nodes"]:
                graphs += 1
                parent = message["graph"]["nodes"]["wavparse0"]["parent"]
                print("graph: wavparse0 is in the graph, inside", parent)
            elif message["type"] == "model" and "wavparse0" in message["model"]["elements"]:
                models += 1
                print("model: wavparse0 is exposed, with", message["values"])


# --8<-- [end:watch]


async def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        wav = Path(folder) / "tone.wav"
        recorder = Player.from_description(
            f"audiotestsrc num-buffers=5 ! wavenc ! filesink location={wav}"
        )
        async with recorder:
            await recorder.play_until_eos()

        # expose_all: decodebin's own elements are never named, so they only show up this way
        description = f"filesrc location={wav} ! decodebin name=dec ! fakesink"
        player = Player.from_description(description, expose_all=True)
        async with player, serve(player, "127.0.0.1", 0) as listening:
            await watch(listening.url.replace("http://", "ws://"))


asyncio.run(main())
