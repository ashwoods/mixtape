"""A client of the HTTP API that `mixtape run --http` serves: what `mixtape get`, `set`, `call`
and `watch` send. The model at `/api/model` says which properties and commands there are, and
the server validates every value, so this needs neither GStreamer nor the model's classes, and
nothing but the standard library (`watch` excepted: it needs the websockets of the http extra).
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from typing import Any, Literal

from .hints import property_hint

logger = logging.getLogger(__name__)

TIMEOUT = 30.0
"""Seconds to wait for an answer: more than a `stop` with an EOS takes with its defaults (5 s for
the EOS, 5 s for the state). A command with a `timeout` argument gets it on top."""
LIST_HINT = "mixtape get URL lists them"
WS_TRY_AGAIN_LATER = 1013
"""The close code of the server for a client that fell too far behind its events."""


class ClientError(Exception):
    """What went wrong, as the server said it (its `detail`, `code` and `hint`, and the HTTP
    `status`), or as the client found it: a name the model does not have (status 404, as the
    server's), a server it cannot reach (`connection_failed`) or that is not mixtape's
    (`bad_response`), with no status."""

    def __init__(
        self, message: str, code: str, hint: str | None = None, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint
        self.status = status


def _unreachable(url: str, reason: object) -> ClientError:
    return ClientError(
        f"Cannot reach {url}: {reason}",
        "connection_failed",
        "is `mixtape run --http PORT` running there?",
    )


def _command_timeout(args: dict[str, Any]) -> float | None:
    """How long to wait for a command: longer by its own `timeout`, forever when that is null."""
    if "timeout" not in args:
        return TIMEOUT
    wait = args["timeout"]
    if wait is None:
        return None
    if isinstance(wait, int | float) and not isinstance(wait, bool) and wait >= 0:
        return TIMEOUT + wait
    return TIMEOUT  # not a number of seconds: the server refuses it at once


class Client:
    """The player served at `url`, `http://host:port`."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self._model: dict[str, Any] | None = None

    def request(
        self, method: str, path: str, body: Any = None, *, timeout: float | None = TIMEOUT
    ) -> Any:
        """The JSON of the answer to `method path` with `body` as JSON. A refusal of the server
        raises its error, as ClientError."""
        url = self.url + path
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        logger.info("%s %s%s", method, url, f" {data.decode()}" if data else "")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except (http.client.HTTPException, OSError):
                raw = b""
            raise self._refusal(exc.code, raw) from None
        except (urllib.error.URLError, OSError) as exc:  # refused, unknown host, timed out
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise ClientError(
                    f"{self.url} did not answer {method} {path} within {timeout:g} s",
                    "timeout",
                    "it may still be doing it: `mixtape get URL` shows the values",
                ) from None
            raise _unreachable(self.url, reason) from None
        except http.client.HTTPException as exc:  # it does not speak HTTP: an SSH server
            raise self._bad_response(f"{type(exc).__name__}: {exc}") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise self._bad_response(f"its answer to {method} {path} is not JSON") from None

    def _refusal(self, status: int, raw: bytes) -> ClientError:
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not (
            isinstance(body, dict)
            and isinstance(body.get("detail"), str)
            and isinstance(body.get("code"), str)
        ):  # not one of mixtape's errors: another server, or a path it does not serve
            return self._bad_response(f"HTTP {status}")
        return ClientError(body["detail"], body["code"], body.get("hint"), status)

    def _bad_response(self, why: str) -> ClientError:
        return ClientError(
            f"{self.url} did not answer as mixtape does: {why}",
            "bad_response",
            "is it a `mixtape run --http` server?",
        )

    def model(self) -> dict[str, Any]:
        """The model, as `/api/model` has it, read once."""
        if self._model is None:
            model = self.request("GET", "/api/model")
            if not (isinstance(model, dict) and isinstance(model.get("commands"), dict)):
                raise self._bad_response("/api/model is not a player's model")
            self._model = model
        return self._model

    def _locate(self, path: str, access: Literal["read", "write"]) -> tuple[str | None, str]:
        """The element key (None for the player's own properties) and the name a REST endpoint
        knows the property at `path` by. Element keys can have dots, so it is looked up rather
        than split."""
        model = self.model()
        if path in model.get("properties", {}):
            return None, path
        for key, element in model.get("elements", {}).items():
            for name, prop in element["properties"].items():
                if prop["path"] == path:
                    return key, name
        hint = property_hint(model, path, access) or LIST_HINT
        raise ClientError(f"Unknown property {path!r}", "unknown_name", hint, 404)

    def _endpoint(self, key: str | None) -> str:
        if key is None:
            return "/api/properties"
        return f"/api/elements/{urllib.parse.quote(key, safe='')}"

    def paths(self) -> Iterator[str]:
        """The paths of the readable properties, in the model's order."""
        model = self.model()
        props = [*model.get("properties", {}).values()]
        props += [p for e in model.get("elements", {}).values() for p in e["properties"].values()]
        return (p["path"] for p in props if p["readable"])

    def get(self, paths: Iterable[str]) -> dict[str, Any]:
        """`{path: value}` of the `paths`, with one request per endpoint."""
        located = {path: self._locate(path, "read") for path in paths}  # all, before any read
        read: dict[str | None, dict[str, Any]] = {}
        values = {}
        for path, (key, name) in located.items():
            if key not in read:
                read[key] = self.request("GET", self._endpoint(key))
            if name not in read[key]:  # the endpoints leave the write-only ones out
                raise ClientError(f"{path!r} is write-only", "not_readable", None, 422)
            values[path] = read[key][name]
        return values

    def set(self, assignments: Iterable[tuple[str, Any]]) -> dict[str, Any]:
        """Set the values in order, stopping at the first error: those before it stay set.
        Returns `{path: value}` as the server reads them back."""
        located = [(path, self._locate(path, "write"), value) for path, value in assignments]
        values = {}
        for path, (key, name), value in located:
            read = self.request("PATCH", self._endpoint(key), {name: value})
            if name in read:  # not when write-only
                values[path] = read[name]
        return values

    def call(self, name: str, args: dict[str, Any]) -> Any:
        """The result of the command `name` with `args`."""
        path = f"/api/commands/{urllib.parse.quote(name, safe='')}"
        # an unknown one is the server's 404, with the hint `hints.command_hint` gives
        answer = self.request("POST", path, args, timeout=_command_timeout(args))
        if not isinstance(answer, dict) or "result" not in answer:
            raise self._bad_response(f"its answer to POST {path} has no result")
        return answer["result"]

    def events_url(self) -> str:
        """The URL of the `/api/events` WebSocket."""
        scheme, rest = self.url.split("://", 1)
        return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/api/events"


async def watch(url: str) -> None:
    """Print every message of the `/api/events` WebSocket of `url` as it comes, one per line,
    until the server closes it. Needs the websockets of the http extra (ImportError)."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosedError, InvalidHandshake, InvalidStatus

    client = Client(url)
    try:
        socket = await connect(client.events_url(), open_timeout=TIMEOUT, max_size=None)
    except InvalidStatus as exc:
        status = exc.response.status_code
        if status == 403:
            raise ClientError(
                f"{client.url} refused the connection (HTTP 403)", "forbidden", None, 403
            ) from None
        raise client._bad_response(f"HTTP {status} for the WebSocket /api/events") from None
    except InvalidHandshake as exc:
        raise client._bad_response(str(exc)) from None
    except (OSError, TimeoutError) as exc:
        raise _unreachable(client.url, exc) from None
    async with socket:
        try:
            async for message in socket:
                text = message if isinstance(message, str) else message.decode()
                print(text, flush=True)
        except ConnectionClosedError as exc:  # not 1000 or 1001: uvicorn's shutdown is 1012
            if exc.rcvd is None:  # the connection broke: no close frame
                raise ClientError(
                    f"The connection to {client.url} broke", "connection_failed"
                ) from None
            if exc.rcvd.code == WS_TRY_AGAIN_LATER:  # events were lost: not a normal end
                raise ClientError(
                    f"{client.url} closed the connection: this client fell behind",
                    "connection_failed",
                    "watch again to get the model and the values again",
                ) from None
