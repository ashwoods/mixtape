"""The `/api/events` WebSocket: the player's events out, commands and sets in.

Server to client, one JSON object per message:

    {"type": "model", "model": {…}}                 first, as `/api/model`
    {"type": "values", "values": {path: value}}      then every readable property
    {"type": "graph", "graph": {…}}                 then the graph as `/api/graph`, and whenever
                                                     the pipeline's shape changes
    {"type": "model", "model", "values"}             the exposed elements changed: `values` has
                                                     the readable properties of the new ones
    {"type": "state", "old", "new", "pending"}       the pipeline's state (`void-pending`)
    {"type": "property", "path", "element", "property", "value"}   (null for a computed one)
    {"type": "eos"}
    {"type": "error" | "warning", "source", "message", "debug"}   (an error also once on connect)
    {"type": "position", "position", "duration"}
    {"type": "commands", "unavailable": {name: reason}}   the commands that cannot run changed
    {"type": "reply", "id", "result"}                 or "error": {"status", "detail", "code",
                                                     "hint"[, "errors"]}, as REST's errors

Client to server:

    {"id": any, "command": name, "args": {…}}         invoke a command
    {"id": any, "set": {path: value, …}}              set properties in order

NaN and infinities are sent as null, as REST sends them. A client that stays more than
`SEND_BUFFER` messages behind for `SEND_GRACE` seconds, or falls `SEND_BYTES` behind, is
dropped, with a close 1013 when it still reads: it gets the snapshot again when it reconnects.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ...exceptions import InvalidValueError, MixTapeError
from ...player import Player
from ..encoding import encode_json
from ..messages import EventEncoder
from ..values import read_values, set_values
from .errors import INTERNAL_ERROR, describe_error
from .security import check_headers

logger = logging.getLogger(__name__)

WS_POLICY_VIOLATION = 1008
WS_GOING_AWAY = 1001
WS_TRY_AGAIN_LATER = 1013
SEND_BUFFER = 256
"""The messages a client may fall behind by, past the socket's own buffers: they wait for it in
memory."""
SEND_GRACE = 2.0
"""Seconds a client more than `SEND_BUFFER` behind has to catch up: a burst of events puts it
that far behind in one go, however fast it reads."""
SEND_BYTES = 16 * 1024 * 1024
"""The bytes a client may fall behind by, grace or not."""
CLOSE_TIMEOUT = 2.0
"""Seconds to send the close frame: a client that does not read never takes it."""


async def _dispatch(player: Player, message: Any) -> Any:
    """Run a client message and return its result."""
    if not isinstance(message, dict):
        raise InvalidValueError("A message must be a JSON object")
    if "command" in message:
        name, args = message["command"], message.get("args") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            raise InvalidValueError('"command" must be a name and "args" an object')
        return await player.invoke(name, **args)
    if "set" in message:
        values = message["set"]
        if not isinstance(values, dict):
            raise InvalidValueError('"set" must be an object of path: value')
        await set_values(player, values)
        return read_values(player, {path: path for path in values})
    raise InvalidValueError('A message needs "command" or "set"')


async def serve_events(websocket: WebSocket, player: Player, *, loopback: bool) -> None:
    """Serve one client until it disconnects or the player ends its events."""
    if (problem := check_headers(websocket.headers, loopback=loopback)) is not None:
        await websocket.close(code=WS_POLICY_VIOLATION, reason=problem)
        return
    await websocket.accept()
    stream = player.events(position=True)  # before the snapshot: nothing falls between them
    encoder = EventEncoder(player)
    snapshot = encoder.snapshot()
    outbox: asyncio.Queue[str] = asyncio.Queue()  # one writer: whole and in the order sent
    queued = 0  # the bytes in it: JSON as `json.dumps` writes it is ASCII
    behind = asyncio.Event()  # the client cannot keep up
    grace: asyncio.TimerHandle | None = None  # it is more than SEND_BUFFER behind: till when

    def send(text: str) -> None:
        nonlocal queued, grace
        outbox.put_nowait(text)
        queued += len(text)
        if queued > SEND_BYTES:
            behind.set()
        elif grace is None and outbox.qsize() > SEND_BUFFER:
            grace = asyncio.get_running_loop().call_later(SEND_GRACE, behind.set)

    async def write() -> None:
        nonlocal queued, grace
        while True:
            text = await outbox.get()
            try:
                await websocket.send_text(text)
            except (WebSocketDisconnect, RuntimeError):
                return  # the client left
            outbox.task_done()
            queued -= len(text)
            if grace is not None and outbox.qsize() <= SEND_BUFFER:
                grace.cancel()  # it caught up
                grace = None

    async def forward() -> None:
        async for event in stream:
            if (message := encoder.encode(event)) is None:
                continue
            try:
                text = encode_json(message)
            except ValueError:
                logger.exception("Could not send %r", event)
                continue
            send(text)
            if behind.is_set():
                return  # it is dropped: encode no more for it
        await outbox.join()  # the player is done: its last events go out before the close

    commands: set[asyncio.Task[None]] = set()

    async def handle(data: str | bytes) -> None:
        reply: dict[str, Any] = {"type": "reply", "id": None}
        try:
            try:
                message = json.loads(data)
            except ValueError as exc:
                raise InvalidValueError(f"Invalid JSON: {exc}") from exc
            if isinstance(message, dict):
                reply["id"] = message.get("id")
            reply["result"] = await _dispatch(player, message)
            text = encode_json(reply)  # a result JSON cannot carry (an object) fails here
        except Exception as exc:
            reply.pop("result", None)
            if isinstance(exc, MixTapeError):
                status, body = describe_error(exc)
                reply["error"] = {"status": status, **body}
            else:
                logger.exception("A WebSocket message failed")
                reply["error"] = {"status": 500, **INTERNAL_ERROR}
            text = encode_json(reply)
        # after the events the command published: forward's turn to send them is already due
        await asyncio.sleep(0)
        send(text)

    async def receive() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            # a command awaits (`play`, `eos`): as a task, so the events keep streaming
            task = asyncio.create_task(handle(message.get("text") or message.get("bytes") or ""))
            commands.add(task)
            task.add_done_callback(commands.discard)

    tasks: list[asyncio.Task[Any]] = []
    try:
        for message in snapshot:  # first, before any event or reply
            send(encode_json(message))
        tasks = [asyncio.create_task(c) for c in (forward(), receive(), write(), behind.wait())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stream.close()
        if grace is not None:
            grace.cancel()
        pending: list[Awaitable[object]] = [*tasks, *commands]
        for task in (*tasks, *commands):
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if websocket.application_state == WebSocketState.CONNECTED:
            if behind.is_set():
                logger.warning(
                    "A client fell %d messages, %d bytes behind: closing it",
                    outbox.qsize(),
                    queued,
                )
            code = WS_TRY_AGAIN_LATER if behind.is_set() else WS_GOING_AWAY
            try:
                await asyncio.wait_for(websocket.close(code=code), CLOSE_TIMEOUT)
            except (WebSocketDisconnect, RuntimeError, TimeoutError):
                pass  # the client left meanwhile, or does not read
