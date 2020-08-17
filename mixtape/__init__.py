from .core import BoomBox

from typing import (Any, Callable, List, Mapping, MutableMapping, Optional,
                    Tuple, Type, TypeVar)

import pluggy

from . import hookspecs

hookimpl = pluggy.HookimplMarker("mixtape")

def load_plugin_manager(plugins=None):
    """Init mixtape plugin manager"""

    if plugins is None:
        plugins = []
    pm = pluggy.PluginManager(__name__)
    pm.add_hookspecs(hookspecs)
    pm.load_setuptools_entrypoints(group=__name__) 
    return pm