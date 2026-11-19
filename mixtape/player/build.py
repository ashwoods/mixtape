"""The player's model: its commands and computed properties, the description of them, the
graph of its pipeline, and the elements, keys and paths the model reads and writes by."""

import logging
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from typing import Any, Literal, NamedTuple, Protocol

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .. import hints
from ..exceptions import UnknownNameError
from ..features import Feature
from ..model import (
    BUILTIN_PROPS,
    Codec,
    Command,
    Computed,
    Declarations,
    action_signals,
    codec_for,
    describe_element,
    element_prop_spec,
    factory_name,
    is_user_named,
)
from ..spec import GraphSpec, LinkSpec, NodeSpec, PlayerSpec
from .util import _retrying

logger = logging.getLogger("mixtape.player")  # the player's: the CLI filters on its name


def _real_peer(pad: Gst.Pad) -> Gst.Pad | None:
    """The pad of an element that is not a bin that `pad` feeds, through ghost pads."""
    peer = pad.get_peer()
    while peer is not None:
        if isinstance(peer, Gst.GhostPad):  # into a bin
            peer = peer.get_target()
        elif isinstance(ghost := peer.get_parent(), Gst.GhostPad):  # out of one
            peer = ghost.get_peer()
        else:
            return peer
    return None


class _Bound(NamedTuple):
    """A command of one player: the declaration, and what it calls with the arguments."""

    declared: Command
    call: Callable[..., Any]
    owner: Any = None
    """What its check is called with: the player, or the feature the command belongs to"""


class Host(Protocol):
    """What the model needs of its player, read at each use. Its public methods are the
    player's, so a subclass that overrides one is what the model calls."""

    @property
    def pipeline(self) -> Gst.Pipeline: ...

    @property
    def state(self) -> Gst.State: ...

    @property
    def _features(self) -> Mapping[str, Feature]: ...

    def _declarations(self) -> Declarations: ...

    def setup(self) -> None: ...

    def element_keys(self) -> dict[str, Gst.Element]: ...

    def exposed_elements(self) -> dict[str, Gst.Element]: ...

    def describe(self) -> PlayerSpec: ...

    def unavailable(self) -> dict[str, str]: ...


class ModelBuilder:
    """The model of one player: its commands and computed properties, built again when the
    exposed elements change, and what reads the pipeline for them."""

    def __init__(self, host: Host, *, expose_all: bool) -> None:
        self._host = host
        self._expose_all = expose_all
        self._unchecked: set[str] = set()  # the commands whose failing check was logged
        self._commands: dict[str, _Bound] = {}  # until `rebuild`
        self._computed: dict[str, tuple[Computed[Any], object]] = {}

    @property
    def commands(self) -> Mapping[str, _Bound]:
        """The commands of the player by name"""
        return self._commands

    @property
    def computed(self) -> Mapping[str, tuple[Computed[Any], object]]:
        """The computed properties of the player by path, with what they read"""
        return self._computed

    def rebuild(self) -> None:
        """Build the commands and computed properties again."""
        self._commands, self._computed = self._build_model()

    def _build_model(self) -> tuple[dict[str, _Bound], dict[str, tuple[Computed[Any], object]]]:
        """The commands and computed properties of this player: its class's, the action
        signals of the exposed elements as `element.signal`, then each feature's as
        `feature.name`."""

        def bound(c: Command, path: str, call: Callable[..., Any], owner: Any = None) -> _Bound:
            return _Bound(replace(c, spec=c.spec.model_copy(update={"name": path})), call, owner)

        declared = self._host._declarations()
        commands = {
            name: _Bound(c, getattr(self._host, name), self._host)
            for name, c in declared.commands.items()
        }
        for key, element in self._host.exposed_elements().items():
            for name, (c, emit) in action_signals(element).items():
                commands[f"{key}.{name}"] = bound(c, f"{key}.{name}", emit)
        computed: dict[str, tuple[Computed[Any], object]] = {
            name: (c, self._host) for name, c in declared.computed.items()
        }
        for feature in self._host._features.values():
            for name, c in feature._declared.commands.items():
                path = f"{feature.name}.{name}"
                commands[path] = bound(c, path, feature._command(name), feature)
            for name, p in feature._declared.computed.items():
                computed[f"{feature.name}.{name}"] = (p, feature)
        return commands, computed

    def describe(self) -> PlayerSpec:
        properties = dict(BUILTIN_PROPS)
        for name, alias in self._host._declarations().props.items():
            element, ps, codec = self.locate(alias.target, exposed=False)
            key = alias.target.rpartition(".")[0]  # what `locate` found it by
            properties[name] = alias.spec(element_prop_spec(element, key, ps, codec))
        for path, (computed, _) in self._computed.items():
            properties[path] = computed.spec(path)
        return PlayerSpec(
            name=type(self._host).__name__,
            features=list(self._host._features),
            commands={name: bound.declared.spec for name, bound in self._commands.items()},
            properties=properties,
            elements={
                key: describe_element(e, key) for key, e in self._host.exposed_elements().items()
            },
            unavailable=self._host.unavailable(),
        )

    def unavailable(self) -> dict[str, str]:
        refused = {name: self.refusal(name, bound) for name, bound in self._commands.items()}
        return {name: reason for name, reason in refused.items() if reason is not None}

    def refusal(self, name: str, bound: _Bound) -> str | None:
        """Why the command `name` cannot run now, else None. A check that raises, or returns
        something else than True, False or a reason, refuses it as an error."""
        when = bound.declared.when
        if when is None:
            return None
        if bound.owner is not self._host and bound.owner.player is None:
            return "The feature is not attached to a running player"  # before setup: inspect
        try:
            verdict = when(bound.owner)
            if not isinstance(verdict, bool) and not (isinstance(verdict, str) and verdict):
                raise TypeError(f"it returned {verdict!r}, not True, False or a reason")
        except Exception as exc:
            if name not in self._unchecked:
                self._unchecked.add(name)
                logger.exception("The check of the command %r failed", name)
            return f"{name!r} could not be checked: {exc}"
        self._unchecked.discard(name)
        if isinstance(verdict, str):
            return verdict
        return None if verdict else f"{name!r} cannot run now"

    def graph(self) -> GraphSpec:
        self._host.setup()
        return self.graph_of(self._host.element_keys())

    def graph_of(self, element_keys: dict[str, Gst.Element]) -> GraphSpec:
        keys = {e: key for key, e in element_keys.items()}
        pipeline_key = keys.pop(self._host.pipeline)
        nodes = {
            key: NodeSpec(
                name=e.get_name(),
                factory=factory_name(e),
                parent=keys.get(e.get_parent()),
                bin=isinstance(e, Gst.Bin),
                exposed=self.is_exposed(e),
            )
            for e, key in keys.items()
        }
        links = []
        for element, key in keys.items():
            if isinstance(element, Gst.Bin):
                continue
            for pad in _retrying(element.iterate_src_pads):
                peer = _real_peer(pad)
                # once: a streaming thread may unlink it meanwhile
                if peer is None or (sink := peer.get_parent_element()) not in keys:
                    continue
                caps = pad.get_current_caps()
                links.append(
                    LinkSpec(
                        source=key,
                        source_pad=pad.get_name(),
                        sink=keys[sink],
                        sink_pad=peer.get_name(),
                        caps=caps.to_string() if caps is not None else None,
                    )
                )
        return GraphSpec(pipeline=pipeline_key, nodes=nodes, links=links)

    def element_keys(self) -> dict[str, Gst.Element]:
        def walk(bin: Gst.Bin) -> Iterator[Gst.Element]:
            # in the order they were added, sources first in a launch line (bins list
            # their newest child first), each bin before its children
            for child in reversed(_retrying(bin.iterate_elements)):
                yield child
                if isinstance(child, Gst.Bin):
                    yield from walk(child)

        # a streaming thread adding elements (decodebin) makes the walk RESYNC
        elements = (self._host.pipeline, *_retrying(lambda: walk(self._host.pipeline)))
        counts = Counter(e.get_name() for e in elements)
        keys: dict[str, Gst.Element] = {}
        for element in elements:
            names = [element.get_name()]
            if element is not self._host.pipeline and counts[names[0]] > 1:
                parent = element.get_parent()
                while parent is not None and parent != self._host.pipeline:
                    names.insert(0, parent.get_name())
                    parent = parent.get_parent()
                if names == [self._host.pipeline.get_name()]:
                    names.insert(0, names[0])
            keys[".".join(names)] = element
        return keys

    def exposed_elements(self) -> dict[str, Gst.Element]:
        return {key: e for key, e in self._host.element_keys().items() if self.is_exposed(e)}

    def is_exposed(self, element: Gst.Element) -> bool:
        return self._expose_all or is_user_named(element)

    def resolve(
        self, path: str, access: Literal["read", "write"]
    ) -> tuple[Gst.Element, Any, Codec]:
        """Like `locate`, following an alias to its target, which is always exposed. An
        unknown property gets the close ones that can be read or written, as `access` says,
        as its hint."""
        alias = self._host._declarations().props.get(path)
        if alias:
            return self.locate(alias.target, exposed=False)
        try:
            return self.locate(path)
        except UnknownNameError as exc:
            if str(exc) == f"Unknown property {path!r}":  # not ambiguous or not exposed
                model = self._host.describe().model_dump(mode="json")
                exc.hint = hints.property_hint(model, path, access)
            raise

    def locate(self, path: str, *, exposed: bool = True) -> tuple[Gst.Element, Any, Codec]:
        """The element, ParamSpec and codec of an `element.property` path."""
        name, _, prop_name = path.rpartition(".")
        keys = self._host.element_keys()
        element = keys.get(name)
        if element is None and name:
            same = sorted(key for key, e in keys.items() if e.get_name() == name)
            if len(same) > 1:
                raise UnknownNameError(f"{name!r} is ambiguous: use {' or '.join(same)}")
        ps = element.find_property(prop_name) if element else None
        codec = codec_for(ps) if ps else None
        if element is None or ps is None or codec is None:
            raise UnknownNameError(f"Unknown property {path!r}")
        if exposed and not self.is_exposed(element):
            raise UnknownNameError(f"{name!r} is not exposed: name it or use expose_all")
        return element, ps, codec

    def read_builtin(self, path: str) -> Any:
        if path == "state":
            return str(self._host.state.value_nick)
        query = (
            self._host.pipeline.query_position
            if path == "position"
            else self._host.pipeline.query_duration
        )
        ok, value = query(Gst.Format.TIME)
        return value / Gst.SECOND if ok and value >= 0 else None
