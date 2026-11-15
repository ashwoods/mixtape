"""Run the app with uvicorn in the current asyncio loop, next to the player."""

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import uvicorn

from ...player import Player
from .app import create_app


class Server(uvicorn.Server):
    """A uvicorn server that leaves the signals to its caller: uvicorn would take over
    SIGINT and SIGTERM with `signal.signal`, and the player's owner handles those itself.
    Stop it with `should_exit = True`."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@dataclass(frozen=True)
class Listening:
    """Where a running server listens."""

    host: str
    port: int
    """The port it got, when 0 was asked for"""

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


def _bind(host: str, port: int) -> socket.socket:
    """Bind the listening socket ourselves: a port in use is an OSError here, where uvicorn
    would log it and `sys.exit`."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host, port), family=family)


@contextlib.asynccontextmanager
async def serve(
    player: Player, host: str, port: int, *, log_level: int | str = "warning"
) -> AsyncIterator[Listening]:
    """Serve `player` over HTTP in the running loop until the block ends.

    It returns once the server listens, with where it does (`port` may be 0). It raises
    `OSError` when the address cannot be bound. `log_level` is uvicorn's own; its loggers
    use the logging configuration of the process."""
    sock = _bind(host, port)
    config = uvicorn.Config(
        create_app(player, host=host),
        log_level=log_level,
        log_config=None,
        lifespan="off",
        timeout_graceful_shutdown=5,
    )
    server = Server(config)
    task = asyncio.create_task(server.serve([sock]))
    try:
        while not server.started:
            if task.done():
                await task  # it ended before it listened: raise why
                raise RuntimeError("The HTTP server stopped before it started")
            await asyncio.sleep(0.01)
        yield Listening(host, sock.getsockname()[1])
    finally:
        server.should_exit = True
        await task
        sock.close()
