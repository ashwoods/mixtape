"""The `gst:` features: any GStreamer tracer as a feature, its properties and action signals in
the model, with no Python of its own (`--feature gst:queue-levels`)."""

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GObject

from ..exceptions import FeatureError
from ..model import Computed, Declarations, GObjectProp, action_signals, exposed_props
from . import GST, Feature
from .tracers import active_tracers, add_tracers, require

if TYPE_CHECKING:
    from ..player import Player

logger = logging.getLogger(__name__)

_FACTORY = re.compile(r"[a-z0-9][a-z0-9_-]*")
REFUSED = {
    "leaks": "use the feature 'leaks': the tracer's signals crash the process or corrupt "
    "reference counts when called out of order (activity-get-checkpoint before "
    "activity-start-tracking, get-live-objects from Python)",
}
"""Tracers `gst:` refuses, and why"""


class GstTracer(Feature):
    """A GStreamer tracer as a feature, `gst:NAME[,key=value…]`: a core one or one of a plugin
    on `GST_PLUGIN_PATH`, in any language.

    The options are the tracer's parameters: `gst:queue-levels,file=x.csv` adds
    `queue-levels(file=x.csv)` to `GST_TRACERS` before GStreamer is initialised (an entry the
    environment has for it wins, with a warning). In the model, as `gst:NAME.x`:

    - its readable properties are read-only properties, published when it notifies them;
    - its action signals are commands, those whose parameters and result the model supports
      (see `mixtape.model.action_signals`), with the parameters `arg1`, `arg2`...

    A tracer exists only when GStreamer is initialised with it, once per process: build the
    player with `Player.from_description` or `mixtape run`, first in the process. A tracer that
    an enabled feature reads itself (`perf`'s) is refused, and so is `leaks`.
    """

    name = "gst"

    def __init__(self, factory: str, params: dict[str, str] | None = None) -> None:
        if _FACTORY.fullmatch(factory) is None:
            raise FeatureError(f"Invalid tracer name {factory!r} in '{GST}{factory}'")
        if (why := REFUSED.get(factory)) is not None:
            raise FeatureError(f"The feature '{GST}{factory}' is not supported: {why}")
        self.name = f"{GST}{factory}"
        self.factory = factory
        """The tracer's factory name"""
        items = ",".join(f"{key}={value}" for key, value in (params or {}).items())
        self.entry = f"{factory}({items})" if items else factory
        """Its `GST_TRACERS` entry"""
        self._declared = Declarations({}, {}, {})
        self._calls: dict[str, Callable[..., Any]] = {}
        self._tracer: Any = None
        self._handler: int | None = None

    def prepare(self) -> None:
        for asked, kept in add_tracers([self.entry]).items():
            logger.warning("GST_TRACERS already has %r: keeping it rather than %r", kept, asked)

    def initialised(self) -> None:
        self._tracer = active_tracers().get(self.factory)
        if self._tracer is None:
            return  # `check` says why
        computed: dict[str, Computed[Any]] = {
            ps.name: GObjectProp(self._tracer, ps, codec)
            for ps, codec in exposed_props(self._tracer)
            if ps.flags & GObject.ParamFlags.READABLE
        }
        signals = action_signals(self._tracer)
        self._declared = Declarations({}, {n: c for n, (c, _) in signals.items()}, computed)
        self._calls = {n: emit for n, (_, emit) in signals.items()}

    def check(self, player: "Player") -> None:
        for other in player.enabled_features.values():
            if self.factory in other.own_tracers:
                raise FeatureError(
                    f"The feature {self.name!r} cannot run beside the feature {other.name!r}, "
                    f"which reads the {self.factory!r} tracer itself: there is one of it per "
                    "process"
                )
        require(self.name, [self.factory], self.name)

    def attach(self, player: "Player") -> None:
        props = self._declared.computed

        def notified(_tracer: Any, ps: Any) -> None:  # on whatever thread changed it
            if ps.name in props:
                player._call_soon(self.notify, ps.name)

        self._handler = self._tracer.connect("notify", notified)

    def detach(self, player: "Player") -> None:
        if self._handler is not None:
            self._tracer.disconnect(self._handler)
            self._handler = None

    def _command(self, name: str) -> Callable[..., Any]:
        return self._calls[name]
