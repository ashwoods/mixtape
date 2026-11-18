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
    {"type": "reply", "id", "result"}                 or "error": {"status", "detail"[, "errors"]}

Client to server:

    {"id": any, "command": name, "args": {…}}         invoke a command
    {"id": any, "set": {path: value, …}}              set properties in order

NaN and infinities are sent as null, as REST sends them. A client that stays more than
`SEND_BUFFER` messages behind for `SEND_GRACE` seconds, or falls `SEND_BYTES` behind, is
dropped, with a close 1013 when it still reads: it gets the snapshot again when it reconnects.
"""

import asyncio
import json
import math
import logging
from collections.abc import Awaitable
from typing import Any

from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ... import events
from ...exceptions import InvalidValueError, MixTapeError
from ...exceptions import UnknownNameError
from ...model import PlayerSpec
from ...player import Player
from .errors import describe_error
from .security import check_headers
from .values import all_values, read_values, set_values

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


def encode_event(event: events.Event) -> dict[str, Any] | None:
    """The JSON message of an event, None for those the page has no use for."""
    match event:
        case events.StateChanged(old=old, new=new, pending=pending):
            return {
                "type": "state",
                "old": old.value_nick,
                "new": new.value_nick,
                "pending": pending.value_nick,
            }
        case events.PropertyChanged(path=path, element=element, property=prop, value=value):
            return {
                "type": "property",
                "path": path,
                "element": element,
                "property": prop,
                "value": value,
            }
        case events.Eos():
            return {"type": "eos"}
        case events.Error(error=error):
            return {
                "type": "error",
                "source": error.source,
                "message": error.message,
                "debug": error.debug,
            }
        case events.Warning(source=source, message=message, debug=debug):
            return {"type": "warning", "source": source, "message": message, "debug": debug}
        case events.Position(position=position, duration=duration):
            return {"type": "position", "position": position, "duration": duration}
        case events.CommandsChanged(unavailable=unavailable):
            return {"type": "commands", "unavailable": unavailable}
        case events.GraphChanged(graph=graph):
            return {"type": "graph", "graph": graph.model_dump(mode="json")}
        case events.ModelChanged(model=model):
            return {"type": "model", "model": model.model_dump(mode="json")}
    return None  # Message


def _element_values(player: Player, model: PlayerSpec, keys: set[str]) -> dict[str, Any]:
    """`{path: value}` of the readable properties of the elements `keys` of `model`."""
    values: dict[str, Any] = {}
    for key in keys:
        paths = [p.path for p in model.elements[key].properties.values() if p.readable]
        try:
            values |= read_values(player, {path: path for path in paths})
        except UnknownNameError:
            continue  # gone again since the model, or renamed by a namesake: a later one says
    return values


def _finite(value: Any) -> Any:
    """`value`, made of what `jsonable_encoder` returns, with every NaN and infinity in it as
    None: JSON has no such numbers."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def jsonable(value: Any) -> Any:
    """`value` as data JSON can carry, with NaN and infinities as None, as the properties of
    REST come (pydantic's way)."""
    return _finite(jsonable_encoder(value))


def encode_json(message: Any) -> str:
    """The JSON text of `jsonable(message)`. A value JSON cannot carry (an object) raises
    ValueError."""
    return json.dumps(jsonable(message), allow_nan=False)


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
    model = player.describe()
    snapshot = [
        {"type": "model", "model": model.model_dump(mode="json")},
        {"type": "values", "values": all_values(player)},
        {"type": "graph", "graph": player.graph().model_dump(mode="json")},
    ]
    if player.error is not None and (error := encode_event(events.Error(player.error))):
        snapshot.append(error)  # the page may load after the error that stopped the pipeline
    known = set(model.elements)  # the elements whose values the client has
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
        nonlocal known
        async for event in stream:
            if (message := encode_event(event)) is None:
                continue
            if isinstance(event, events.ModelChanged):
                elements = set(event.model.elements)
                message["values"] = _element_values(player, event.model, elements - known)
                known = elements
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
                reply["error"] = {"status": 500, "detail": "Internal error"}
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
