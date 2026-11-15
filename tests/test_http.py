import asyncio
import json
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio
import websockets
from pydantic import BaseModel
from websockets.exceptions import InvalidStatus
from websockets.typing import Origin

from mixtape import Player, command
from mixtape.interfaces.http import Listening, serve
from mixtape.interfaces.http.security import check_headers

PIPELINE = "videotestsrc name=src ! queue name=q ! fakesink name=sink"
COLLIDING = "bin.( name=a queue name=q ! fakesink ) bin.( name=b queue name=q ! fakesink )"


class Served:
    def __init__(self, player: Player, listening: Listening, client: httpx.AsyncClient) -> None:
        self.player = player
        self.client = client
        self.url = listening.url
        self.ws_url = f"ws://127.0.0.1:{listening.port}/api/events"


@pytest_asyncio.fixture
async def served(Gst: Any) -> AsyncIterator[Served]:
    player = Player.from_description(PIPELINE)
    async with player, serve(player, "127.0.0.1", 0) as listening:
        async with httpx.AsyncClient(base_url=listening.url) as client:
            yield Served(player, listening, client)


async def message(ws: Any) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


async def until(ws: Any, accept: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """The messages up to and including the first one `accept` takes."""
    seen = []
    while True:
        seen.append(await message(ws))
        if accept(seen[-1]):
            return seen


@pytest.mark.asyncio
async def test_model_and_page(served: Served) -> None:
    response = await served.client.get("/api/model")
    assert response.status_code == 200
    assert response.json() == served.player.describe().model_dump(mode="json")
    assert "schema" in response.json()["elements"]["src"]["properties"]["pattern"]
    page = await served.client.get("/")
    assert page.status_code == 200 and "/api/model" in page.text


@pytest.mark.asyncio
async def test_commands(served: Served) -> None:
    client = served.client
    response = await client.post("/api/commands/play")
    assert (response.status_code, response.json()) == (200, {"result": None})
    assert (await client.get("/api/properties")).json()["state"] == "playing"
    assert (await client.post("/api/commands/pause", content=b"")).status_code == 200
    assert served.player.get("state") == "paused"
    # paused, an EOS would block the prerolled sinks and freeze the loop on the next stop
    assert (await client.post("/api/commands/eos")).status_code == 409

    bad = await client.post("/api/commands/seek", json={"position": -1})
    assert bad.status_code == 422
    assert bad.json()["errors"][0]["loc"] == ["position"]
    assert (await client.post("/api/commands/seek", json={})).status_code == 422
    assert (await client.post("/api/commands/seek", content=b"{")).status_code == 422
    assert (await client.post("/api/commands/seek", json=[1])).status_code == 422
    assert (await client.post("/api/commands/nope")).status_code == 404
    await client.post("/api/commands/stop")
    assert served.player.get("state") == "null"


@pytest.mark.asyncio
async def test_elements_and_properties(served: Served) -> None:
    client = served.client
    assert (await client.get("/api/elements/src")).json()["pattern"] == "smpte"
    response = await client.patch("/api/elements/src", json={"pattern": "ball"})
    assert (response.status_code, response.json()) == (200, {"pattern": "ball"})
    assert (await client.get("/api/elements/src")).json()["pattern"] == "ball"

    for element, body, status in [
        ("sink", {"last-message": "x"}, 422),
        ("sink", {"nope": 1}, 404),
        ("src", {"pattern": 99}, 422),
    ]:
        assert (await client.patch(f"/api/elements/{element}", json=body)).status_code == status
    assert (await client.get("/api/elements/nope")).status_code == 404
    assert (await client.patch("/api/elements/nope", json={})).status_code == 404

    # in order, stopping at the first error: what came before stays set
    response = await client.patch("/api/elements/src", json={"pattern": "snow", "is-live": "maybe"})
    assert response.status_code == 422 and response.json()["errors"]
    assert served.player.get("src.pattern") == "snow"

    properties = (await client.get("/api/properties")).json()
    assert set(properties) == {"state", "position", "duration"}
    assert (await client.patch("/api/properties", json={"state": "playing"})).status_code == 422
    assert (await client.patch("/api/properties", json={"src.pattern": "ball"})).status_code == 404


@pytest.mark.asyncio
async def test_dotted_element_keys(Gst: Any) -> None:
    player = Player.from_description(COLLIDING)
    async with player, serve(player, "127.0.0.1", 0) as listening:
        async with httpx.AsyncClient(base_url=listening.url) as client:
            response = await client.patch("/api/elements/a.q", json={"max-size-buffers": 7})
            assert response.json() == {"max-size-buffers": 7}
            assert (await client.get("/api/elements/b.q")).json()["max-size-buffers"] == 200


@pytest.mark.asyncio
async def test_websocket(served: Served) -> None:
    async with websockets.connect(served.ws_url) as ws:
        first = await message(ws)
        assert first["type"] == "values"
        assert first["values"]["state"] == "null" and first["values"]["src.pattern"] == "smpte"
        assert "sink.last-message" in first["values"]

        await ws.send(json.dumps({"id": 1, "set": {"src.pattern": "ball"}}))
        seen = await until(ws, lambda m: m["type"] == "reply")
        assert seen[-1] == {"type": "reply", "id": 1, "result": {"src.pattern": "ball"}}
        if not any(m["type"] == "property" for m in seen):  # the notify may come after the reply
            seen += await until(ws, lambda m: m["type"] == "property")
        event = next(m for m in seen if m["type"] == "property")
        assert event == {
            "type": "property",
            "path": "src.pattern",
            "element": "src",
            "property": "pattern",
            "value": "ball",
        }

        # a command is a task: the events stream while `play` waits for its state change
        await ws.send(json.dumps({"id": "p", "command": "play"}))
        seen = await until(ws, lambda m: m["type"] == "reply")
        states = [m for m in seen if m["type"] == "state"]
        assert states and states[0]["old"] == "null" and "pending" in states[0]
        assert seen[-1] == {"type": "reply", "id": "p", "result": None}
        seen = await until(ws, lambda m: m["type"] == "position" and m["position"] is not None)
        assert seen[-1]["position"] >= 0

        replies: list[tuple[dict[str, Any], int]] = [
            ({"id": 2, "command": "nope"}, 404),
            ({"id": 3, "command": "seek", "args": {"position": -1}}, 422),
            ({"id": 4, "set": {"src.nope": 1}}, 404),
            ({"id": 5, "set": {"state": "null"}}, 422),
            ({"id": 6, "set": [1]}, 422),
            ({"id": 7}, 422),
        ]
        for request, status in replies:
            await ws.send(json.dumps(request))
            reply = (await until(ws, lambda m: m["type"] == "reply"))[-1]
            assert (reply["id"], reply["error"]["status"]) == (request["id"], status)
            assert ("errors" in reply["error"]) == (request["id"] in (3, 6, 7))
        await ws.send("not json")
        reply = (await until(ws, lambda m: m["type"] == "reply"))[-1]
        assert reply["id"] is None and reply["error"]["status"] == 422

        # the socket survived all of that
        await ws.send(json.dumps({"id": 8, "command": "pause"}))
        assert (await until(ws, lambda m: m["type"] == "reply"))[-1]["result"] is None


@pytest.mark.asyncio
async def test_a_disconnect_cancels_the_pending_commands(served: Served) -> None:
    await served.player.play()
    async with websockets.connect(served.ws_url) as ws:
        await message(ws)
        await ws.send(json.dumps({"id": 1, "command": "eos", "args": {"timeout": 30}}))
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.2)
    assert not served.player._eos_waiters  # the cancelled command discarded its waiter


class Point(BaseModel):
    x: int
    y: int = 0


class Draw(Player):
    @command
    def go(self, where: Point, path: list[Point]) -> Point:
        """Go there."""
        return where


@pytest.mark.asyncio
async def test_openapi_has_typed_bodies_and_no_defs(Gst: Any) -> None:
    player = Draw.from_description("fakesink")
    async with player, serve(player, "127.0.0.1", 0) as listening:
        async with httpx.AsyncClient(base_url=listening.url) as client:
            response = await client.get("/openapi.json")
            assert response.status_code == 200
            assert "#/$defs" not in response.text
            body = response.json()["paths"]["/api/commands/go"]["post"]["requestBody"]
            assert (
                body["content"]["application/json"]["schema"]["properties"]["where"]["properties"][
                    "x"
                ]["type"]
                == "integer"
            )
            ok = await client.post("/api/commands/go", json={"where": {"x": "2"}, "path": []})
            assert ok.json() == {"result": {"x": 2, "y": 0}}
            bad = await client.post("/api/commands/go", json={"where": {"x": "a"}, "path": []})
            assert bad.status_code == 422 and bad.json()["errors"][0]["loc"][:2] == ["where", "x"]


@pytest.mark.asyncio
async def test_an_origin_that_is_not_the_host_is_refused(served: Served) -> None:
    host = served.url.removeprefix("http://")
    same = await served.client.get("/api/model", headers={"Origin": f"http://{host}"})
    assert same.status_code == 200  # a page of the server itself
    other = await served.client.get("/api/model", headers={"Origin": "http://evil.example"})
    assert other.status_code == 403
    assert (await served.client.get("/api/model", headers={"Origin": "null"})).status_code == 403

    with pytest.raises(InvalidStatus) as refused:
        async with websockets.connect(served.ws_url, origin=Origin("http://evil.example")):
            pass
    assert refused.value.response.status_code == 403
    async with websockets.connect(served.ws_url, origin=Origin(f"http://{host}")) as ws:
        assert (await message(ws))["type"] == "values"


@pytest.mark.parametrize(
    ("host", "loopback", "allowed"),
    [
        ("127.0.0.1:8080", True, True),
        ("127.0.0.2:8080", True, True),
        ("[::1]:8080", True, True),
        ("localhost:8080", True, True),
        ("192.168.1.5:8080", True, False),
        ("evil.example:8080", True, False),
        ("192.168.1.5:8080", False, True),
        ("localhost:8080", False, True),
        ("evil.example:8080", False, False),  # rebound to the LAN address
    ],
)
def test_the_host_must_name_this_machine(host: str, loopback: bool, allowed: bool) -> None:
    headers = {"host": host, "origin": f"http://{host}"}
    assert (check_headers(headers, loopback=loopback) is None) == allowed


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)


def test_core_imports_without_the_http_extra() -> None:
    code = (
        "import sys; sys.modules['fastapi'] = None; sys.modules['uvicorn'] = None; "
        "import mixtape, mixtape.cli, mixtape.player"
    )
    result = run_python(code)
    assert result.returncode == 0, result.stderr


def test_cli_serves_after_eos_and_stops_on_sigint() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mixtape",
            "-v",
            "run",
            "--http",
            "127.0.0.1:0",
            "videotestsrc num-buffers=10 ! fakesink",
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr = process.stderr
    assert stderr is not None
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        for line in stderr:
            lines.put(line)

    threading.Thread(target=read, daemon=True).start()

    def wait_for(text: str) -> str:
        deadline = time.monotonic() + 10
        while True:
            line = lines.get(timeout=max(deadline - time.monotonic(), 0.01))
            if text in line:
                return line

    try:
        url = wait_for("Serving on ").split("Serving on ")[1].strip()
        assert not url.endswith(":0")  # the port it got
        wait_for("EOS")
        time.sleep(0.2)
        assert process.poll() is None  # an EOS does not end it
        with urllib.request.urlopen(f"{url}/api/model", timeout=5) as response:
            assert json.load(response)["name"] == "Player"
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=5) == 0
    finally:
        process.kill()
        process.wait()
