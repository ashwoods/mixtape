from typing import (
    Any,
    Sequence,
    Optional,
)

import pluggy

from . import hookspecs
from .core import BoomBox, Player

hookimpl = pluggy.HookimplMarker(__name__)

__all__ = ["hookimpl", "hookspecs", "Player", "BoomBox", "load_mixtape_plugins"]


def load_mixtape_plugins(plugins: Optional[Sequence[Any]] = None) -> pluggy.PluginManager:
    """Init mixtape plugin manager"""
    if plugins is None:
        plugins = []
    pm = pluggy.PluginManager(__name__)
    pm.add_hookspecs(hookspecs)
    pm.load_setuptools_entrypoints(group=__name__)
    return pm
