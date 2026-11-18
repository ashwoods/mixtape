"""The wire spec of a player: what `describe()`, `mixtape inspect --json` and `GET /api/model` say.

Plain pydantic models that serialise to JSON with JSON Schema value types, free of GObject
introspection so that clients and tools can import them without GStreamer. `mixtape.model`
builds them from the pipeline's elements and the declarations on `Player` subclasses.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, serialize_by_alias=True)


class PropSpec(_Spec):
    """A readable and/or writable value: an element property, an alias, a computed property
    or a built-in."""

    path: str
    """`enc.bitrate` for element properties (`a.enc.bitrate` when the name is not unique),
    a bare name for aliases, computed properties and built-ins, `feature.name` for those of
    a feature"""
    builtin: bool = False
    """One of the properties every player has: `state`, `position` and `duration`"""
    element: str | None = None
    property: str | None = None
    """The element and its GObject property name, for element properties and aliases"""
    target: str | None = None
    """The element path an alias stands for"""
    value_schema: dict[str, Any] = Field(alias="schema")
    """JSON Schema of the value, serialised as `schema`"""
    readable: bool = True
    writable: bool = True
    controllable: bool = False
    mutable: Literal["playing", "paused", "ready"] | None = None
    """The least restrictive state the element declares for changes. A hint only."""
    description: str | None = None


class CommandSpec(_Spec):
    name: str
    description: str | None = None
    parameters: dict[str, Any]
    """JSON Schema object of the arguments"""
    result: dict[str, Any] | None = None
    builtin: bool = False


class ElementSpec(_Spec):
    name: str
    factory: str
    description: str | None = None
    properties: dict[str, PropSpec]
    """Keyed by GObject property name"""


class PlayerSpec(_Spec):
    name: str
    features: list[str] = Field(default_factory=list)
    """The names of the features the player has enabled, as `mixtape inspect` lists them"""
    commands: dict[str, CommandSpec]
    properties: dict[str, PropSpec]
    """The built-ins, the declared aliases and the computed properties"""
    elements: dict[str, ElementSpec]
    unavailable: dict[str, str] = Field(default_factory=dict)
    """The commands that cannot run now, with the reason; a snapshot as of `describe()`"""


class NodeSpec(_Spec):
    name: str
    factory: str
    parent: str | None = None
    """The key of the bin it is in, None for the pipeline's own children"""
    bin: bool = False
    exposed: bool = False
    """Whether the model has it among its elements"""


class LinkSpec(_Spec):
    """Data flowing between two elements that are not bins, through any bins' ghost pads."""

    source: str
    source_pad: str
    sink: str
    sink_pad: str
    caps: str | None = None
    """The negotiated caps, once there are some"""


class GraphSpec(_Spec):
    """A snapshot of the pipeline's topology, which changes as elements add pads."""

    pipeline: str
    """The pipeline's key: it holds the nodes and is none of them"""
    nodes: dict[str, NodeSpec]
    """Every element under the pipeline by key, bins before their children"""
    links: list[LinkSpec]
