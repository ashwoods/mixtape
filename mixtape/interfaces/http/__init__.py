"""Control a player from a browser: REST, a WebSocket and a page, served by uvicorn in the
player's own asyncio loop. Importing this needs the `http` extra: `pip install 'mixtape[http]'`.

    async with serve(player, "127.0.0.1", 8080) as listening:
        print(listening.url)
        ...
"""

from .app import create_app
from .server import Listening, serve

__all__ = ["Listening", "create_app", "serve"]
