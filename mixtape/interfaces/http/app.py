"""The FastAPI app that exposes a player: REST, a WebSocket and a page.

Every handler is a coroutine on the loop the server runs in, which is the player's. The
REST arguments are validated by the player, not by FastAPI, so a bad value has one error
format everywhere; the OpenAPI schema only documents them."""

import importlib.resources
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from ...exceptions import InvalidValueError, MixTapeError, UnknownNameError
from ...model import ElementSpec
from ...player import Player
from .errors import describe_error
from .security import check_headers, is_loopback
from .values import read_values, set_values
from .ws import serve_events


async def _json_object(request: Request) -> dict[str, Any]:
    """The JSON object in the body; an empty body is `{}`."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise InvalidValueError(f"Invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise InvalidValueError("The body must be a JSON object")
    return body


def create_app(player: Player, *, host: str = "127.0.0.1") -> FastAPI:
    """The app of `player`. `host` is the address the server binds to: on a loopback one,
    requests must also name this machine in their Host header (see `security`)."""
    loopback = is_loopback(host)
    spec = player.describe()
    app = FastAPI(title=spec.name)

    @app.exception_handler(MixTapeError)
    async def mixtape_error(request: Request, exc: MixTapeError) -> JSONResponse:
        status, body = describe_error(exc)
        return JSONResponse(body, status_code=status)

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if (problem := check_headers(request.headers, loopback=loopback)) is not None:
            return JSONResponse({"detail": problem}, status_code=403)
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        page = importlib.resources.files("mixtape.interfaces.http").joinpath("index.html")
        return HTMLResponse(page.read_text(encoding="utf-8"))  # read each time: easy to edit

    @app.get("/api/model")
    async def model() -> JSONResponse:
        return JSONResponse(player.describe().model_dump(mode="json"))

    for command in spec.commands.values():

        def make_route(name: str) -> Callable[[Request], Awaitable[dict[str, Any]]]:
            async def invoke(request: Request) -> dict[str, Any]:
                result = await player.invoke(name, **await _json_object(request))
                return {"result": jsonable_encoder(result)}

            return invoke

        app.add_api_route(
            f"/api/commands/{command.name}",
            make_route(command.name),
            methods=["POST"],
            response_model=None,
            summary=command.name,
            description=command.description,
            responses={
                200: {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"result": command.result or {"type": "null"}},
                            }
                        }
                    }
                }
            },
            openapi_extra={
                "requestBody": {
                    "required": False,
                    "content": {"application/json": {"schema": command.parameters}},
                }
            },
        )

    def element_spec(key: str) -> ElementSpec:
        element = player.describe().elements.get(key)
        if element is None:
            raise UnknownNameError(f"Unknown element {key!r}")
        return element

    def element_values(element: ElementSpec, names: list[str]) -> dict[str, Any]:
        return read_values(player, {name: element.properties[name].path for name in names})

    @app.get("/api/elements/{el}")
    async def get_element(el: str) -> dict[str, Any]:
        element = element_spec(el)
        return element_values(element, [n for n, p in element.properties.items() if p.readable])

    @app.patch("/api/elements/{el}")
    async def patch_element(el: str, request: Request) -> dict[str, Any]:
        element = element_spec(el)
        values = await _json_object(request)

        def path_of(name: str) -> str:
            if name not in element.properties:
                raise UnknownNameError(f"Unknown property {name!r} of element {el!r}")
            return element.properties[name].path

        await set_values(player, values, path_of)
        return element_values(element, list(values))

    def property_values(names: list[str]) -> dict[str, Any]:
        return read_values(player, {name: name for name in names})

    @app.get("/api/properties")
    async def get_properties() -> dict[str, Any]:
        return property_values([n for n, p in player.describe().properties.items() if p.readable])

    @app.patch("/api/properties")
    async def patch_properties(request: Request) -> dict[str, Any]:
        values = await _json_object(request)

        def path_of(name: str) -> str:
            if name not in player.describe().properties:
                raise UnknownNameError(f"Unknown property {name!r}")
            return name

        await set_values(player, values, path_of)
        return property_values(list(values))

    @app.websocket("/api/events")
    async def events(websocket: WebSocket) -> None:
        await serve_events(websocket, player, loopback=loopback)

    return app
