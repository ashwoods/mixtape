"""The `tracers` feature: run GStreamer tracers (core or gst-plugins-rs ones) with the player,
by setting `GST_TRACERS` before GStreamer starts."""

import logging
import os
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst
from pydantic import StringConstraints

from ..exceptions import FeatureError
from ..model import prop
from . import Feature

if TYPE_CHECKING:
    from ..player import Player

logger = logging.getLogger(__name__)

CORE = frozenset({"factories", "latency", "leaks", "rusage", "stats"})
"""GStreamer's own tracers that write what they measure only to the `GST_TRACER` debug log"""
LOG = "GST_TRACER:7"


def _entries(value: str) -> list[str]:
    """The entries of a `GST_TRACERS` value (`name(params);name`)."""
    return [entry for entry in value.split(";") if entry]


def _name(entry: str) -> str:
    return entry.partition("(")[0]


def add_tracers(entries: Iterable[str]) -> dict[str, str]:
    """Add each entry to `GST_TRACERS` whose tracer it does not name yet. The entries it kept
    from the environment instead, where they differ from the one asked for: {asked: kept}."""
    current = _entries(os.environ.get("GST_TRACERS", ""))
    named = {_name(entry): entry for entry in current}
    given = dict(named)
    kept: dict[str, str] = {}
    for entry in entries:
        if _name(entry) not in named:
            named[_name(entry)] = entry
            current.append(entry)
        elif (have := given.get(_name(entry))) is not None and have != entry:
            kept[entry] = have
    os.environ["GST_TRACERS"] = ";".join(current)
    return kept


def add_log() -> str | None:
    """Add `GST_TRACER:7` to `GST_DEBUG`, the output of the core tracers, unless it names
    `GST_TRACER` already. What it kept instead, where that differs."""
    debug = os.environ.get("GST_DEBUG", "")
    ours = [item for item in debug.split(",") if item.partition(":")[0] == "GST_TRACER"]
    if not ours:
        os.environ["GST_DEBUG"] = f"{debug},{LOG}" if debug else LOG
    elif ours != [LOG]:
        return ",".join(ours)
    return None


def active_tracers() -> dict[str, Any]:
    """The tracers GStreamer runs, by the name of their factory."""
    tracers = {tracer.__gtype__: tracer for tracer in Gst.tracing_get_active_tracers()}
    factories = Gst.Registry.get().get_feature_list(Gst.TracerFactory)
    return {
        f.get_name(): tracers[f.get_tracer_type()]
        for f in factories
        if f.get_tracer_type() in tracers
    }


def installed(names: Iterable[str]) -> list[str]:
    """Those of `names` that GStreamer has a tracer of."""
    registry = Gst.Registry.get()
    return [n for n in names if isinstance(registry.lookup_feature(n), Gst.TracerFactory)]


def require(feature: str, names: Iterable[str], spec: str) -> None:
    """Raise `FeatureError` unless GStreamer has and runs a tracer of each of `names`, which
    the feature named `feature` needs; `spec` enables it, as in `--feature spec`."""
    names = list(dict.fromkeys(names))
    found = installed(names)
    if missing := [n for n in names if n not in found]:
        raise FeatureError(
            f"The feature {feature!r} names {', '.join(map(repr, missing))}, which GStreamer "
            "has no tracer of: `gst-inspect-1.0 | grep Tracer` lists them; a plugin of "
            "tracers that GStreamer does not install has to be on GST_PLUGIN_PATH"
        )
    active = active_tracers()
    if inactive := [n for n in names if n not in active]:
        raise FeatureError(
            f"The feature {feature!r} needs the tracers {', '.join(map(repr, inactive))}, but "
            "GStreamer could not create them (its warnings say why) or was initialised "
            "before it could set GST_TRACERS: build the player "
            f"with Player.from_description(..., features=['{spec}']) or run "
            f"`mixtape run --feature {spec}` before anything else initialises "
            "GStreamer, or set GST_TRACERS yourself"
        )


class Tracers(Feature):
    """Run GStreamer tracers with the player, as `GST_TRACERS` names them: `latency`, `rusage`
    and GStreamer's other core tracers, or gst-plugins-rs ones such as `queue-levels`.

    `names` takes the syntax of `GST_TRACERS`: `;` between tracers, parameters in parentheses,
    e.g. `latency(flags=element+pipeline);rusage`. They are added to `GST_TRACERS` before
    GStreamer is initialised; a tracer the environment already names keeps the parameters it
    has there. `log` adds `GST_TRACER:7` to `GST_DEBUG`, the only output of the core tracers:
    by default when `names` has one. A value set in the environment wins over an option, with a
    warning.

    Tracers are created by `Gst.init` and never later, one of each per process: build the
    player with `Player.from_description` or `mixtape run`, first in the process.
    """

    name = "tracers"

    def __init__(
        self,
        names: Annotated[str, StringConstraints(pattern=r"[^;]")],
        log: bool | None = None,
    ) -> None:
        self.names = _entries(names)
        """The `GST_TRACERS` entries, `name(params)`"""
        self.log = any(_name(entry) in CORE for entry in self.names) if log is None else log
        """Whether the `GST_TRACER` debug log is turned on"""

    def prepare(self) -> None:
        for asked, kept in add_tracers(self.names).items():
            logger.warning("GST_TRACERS already has %r: keeping it rather than %r", kept, asked)
        if self.log and (debug := add_log()) is not None:
            logger.warning("GST_DEBUG already has %r: keeping it", debug)

    def check(self, player: "Player") -> None:
        require(self.name, map(_name, self.names), "tracers,names=...")

    @prop
    def active(self) -> list[str]:
        """The tracers GStreamer runs in this process"""
        return sorted(active_tracers())
