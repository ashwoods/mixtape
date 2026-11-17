"""mixtape. Its names load on first use: importing a submodule (the pytest plugin's) must not
import GStreamer, which `Player` does."""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .features import Feature
    from .model import command, prop
    from .player import Player

__all__ = ["Feature", "Player", "command", "prop"]

_HOME = {
    "Feature": "features",
    "Player": "player",
    "command": "model",
    "prop": "model",
}


def __getattr__(name: str) -> Any:
    if (module := _HOME.get(name)) is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value
