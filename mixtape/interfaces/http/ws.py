"""The `/api/events` WebSocket: the player's events out, commands and sets in.

Server to client, one JSON object per message:

    {"type": "values", "values": {path: value}}      first, every readable property
    {"type": "state", "old", "new", "pending"}       the pipeline's state (`void-pending`)
    {"type": "property", "path", "element", "property", "value"}
    {"type": "eos"}
    {"type": "error" | "warning", "source", "message", "debug"}
    {"type": "position", "position", "duration"}
    {"type": "reply", "id", "result"}                 or "error": {"status", "detail"[, "errors"]}

Client to server:

    {"id": any, "command": name, "args": {…}}         invoke a command
    {"id": any, "set": {path: value, …}}              set properties in order
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
from ...player import Player
from .errors import describe_error
from .security import check_headers
from .values import all_values, read_values, set_values

logger = logging.getLogger(__name__)

WS_POLICY_VIOLATION = 1008
WS_GOING_AWAY = 1001


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
    return None  # Message


def _encode(message: dict[str, Any]) -> str:
    """The JSON text of a message, strict as REST is: NaN and infinities raise ValueError."""
    return json.dumps(jsonable_encoder(message), allow_nan=False)


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
    stream = player.events(position=True)  # before the values: nothing falls between them
    lock = asyncio.Lock()  # a reply and an event must not interleave on the socket

    async def send(message: dict[str, Any] | str) -> None:
        text = message if isinstance(message, str) else _encode(message)
        async with lock:
            try:
                await websocket.send_text(text)
            except (WebSocketDisconnect, RuntimeError):
                pass  # the client left: the receiving loop ends the connection

    async def forward() -> None:
        async for event in stream:
            if (message := encode_event(event)) is None:
                continue
            try:
                text = _encode(message)
            except ValueError:
                logger.exception("Could not send %r", event)
                continue
            await send(text)

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
            await send(_encode(reply))  # a result JSON cannot carry fails here
            return
        except MixTapeError as exc:
            status, body = describe_error(exc)
            reply["error"] = {"status": status, **body}
        except Exception:
            logger.exception("A WebSocket message failed")
            reply["error"] = {"status": 500, "detail": "Internal error"}
        reply.pop("result", None)
        await send(reply)

    async def receive() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            # a command awaits (`play`, `eos`): as a task, so the events keep streaming
            task = asyncio.create_task(handle(message.get("text") or message.get("bytes") or ""))
            commands.add(task)
            task.add_done_callback(commands.discard)

    forwarding = asyncio.create_task(forward())
    receiving = asyncio.create_task(receive())
    try:
        values = {
            path: value
            for path, value in all_values(player).items()
            if not (isinstance(value, float) and not math.isfinite(value))  # as forward() does
        }
        await send({"type": "values", "values": values})
        await asyncio.wait({forwarding, receiving}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stream.close()
        tasks: list[Awaitable[object]] = [forwarding, receiving, *commands]
        for task in (forwarding, receiving, *commands):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.close(code=WS_GOING_AWAY)
            except (WebSocketDisconnect, RuntimeError):
                pass  # the client left meanwhile
