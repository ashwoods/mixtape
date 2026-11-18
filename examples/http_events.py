"""A client of the WebSocket /api/events: the first messages, a command and a set with their
replies, and the events they cause.

python3 examples/http_events.py
"""

# --8<-- [start:client]
import asyncio
import json
from typing import Any

import websockets

from mixtape import Player
from mixtape.interfaces.http import serve


async def talk(url: str) -> None:
    async with websockets.connect(f"{url}/api/events") as ws:
        # the first three messages: the model, every readable value, the graph
        for _ in range(3):
            message = json.loads(await ws.recv())
            print("first:", message["type"])
            # > first: model
            # > first: values
            # > first: graph

        await ws.send(json.dumps({"id": 1, "set": {"vol.volume": 0.5}}))
        await ws.send(json.dumps({"id": 2, "command": "no-such-command"}))
        await ws.send(json.dumps({"id": 3, "command": "play"}))

        replies: dict[int, Any] = {}
        changes: list[tuple[str, Any]] = []
        states: list[str] = []
        while len(replies) < 3 or not changes or "playing" not in states:
            message = json.loads(await ws.recv())
            match message["type"]:
                case "reply":
                    replies[message["id"]] = message.get("result", message.get("error"))
                case "property":
                    changes.append((message["path"], message["value"]))
                case "state":
                    states.append(message["new"])
        for id_ in sorted(replies):
            print("reply", id_, replies[id_])
            # > reply 1 {'vol.volume': 0.5}
            # > reply 2 {'status': 404, 'detail': "Unknown command 'no-such-command'", 'code': 'unknown_name', 'hint': None}
            # > reply 3 None
        print("property", changes)
        # > property [('vol.volume', 0.5)]
        print("states", states)
        # > states ['ready', 'paused', 'playing']


# --8<-- [end:client]


async def main() -> None:
    player = Player.from_description("audiotestsrc is-live=true ! volume name=vol ! fakesink")
    async with player, serve(player, "127.0.0.1", 0) as listening:
        await talk(listening.url.replace("http://", "ws://"))


if __name__ == "__main__":
    asyncio.run(main())
