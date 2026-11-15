"""The declarative model of a player: what can be read, set and invoked.

Specs are plain pydantic models that serialise to JSON with JSON Schema value
types. They come from GObject introspection of the pipeline's elements plus the
`prop()` and `@command` declarations on `Player` subclasses. The runtime
bindings (codecs, argument models) stay out of the specs, in this module.
"""

import enum
import functools
import inspect
import operator
import re
import types
import typing
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NamedTuple, Self, overload

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GObject, Gst
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    create_model,
)

# -- specs -- #


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, serialize_by_alias=True)


class PropSpec(_Spec):
    """A readable and/or writable value: an element property, an alias or a built-in."""

    path: str
    """`enc.bitrate` for element properties (`a.enc.bitrate` when the name is not unique),
    a bare name for aliases and built-ins"""
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
    commands: dict[str, CommandSpec]
    properties: dict[str, PropSpec]
    """The built-ins and the declared aliases"""
    elements: dict[str, ElementSpec]


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

    nodes: dict[str, NodeSpec]
    """Every element under the pipeline by key, bins before their children"""
    links: list[LinkSpec]


# -- GObject value types -- #


@dataclass(frozen=True)
class Codec:
    """Validates a value and converts it from and to the GObject representation."""

    adapter: TypeAdapter[Any]
    read: Callable[[Any], Any]
    """A GObject value to a JSON-friendly one"""
    write: Callable[[Any], Any]
    """A validated value to what `set_property` takes"""
    default: Any = None


_MUTABLE: tuple[tuple[Literal["playing", "paused", "ready"], int], ...] = (
    ("playing", Gst.PARAM_MUTABLE_PLAYING),
    ("paused", Gst.PARAM_MUTABLE_PAUSED),
    ("ready", Gst.PARAM_MUTABLE_READY),
)
_INTS = {
    GObject.TYPE_INT,
    GObject.TYPE_UINT,
    GObject.TYPE_LONG,
    GObject.TYPE_ULONG,
    GObject.TYPE_INT64,
    GObject.TYPE_UINT64,
    GObject.TYPE_CHAR,
    GObject.TYPE_UCHAR,
}
_FLOATS = {GObject.TYPE_FLOAT, GObject.TYPE_DOUBLE}


def _parse_caps(text: str) -> Any:
    caps = Gst.Caps.from_string(text)
    if caps is None:
        raise ValueError(f"invalid caps {text!r}")
    return caps


def _parse_structure(text: str) -> Any:
    try:
        structure, rest = Gst.Structure.from_string(text)
    except TypeError:
        raise ValueError(f"invalid structure {text!r}") from None
    if rest.strip():
        raise ValueError(f"invalid structure {text!r}: unexpected {rest.strip()!r}")
    return structure


def _unless_none(parse: Callable[[str], Any]) -> Callable[[str | None], Any]:
    return lambda text: None if text is None else parse(text)


def _to_string(value: Any) -> str | None:
    return None if value is None else str(value.to_string())


def _adapter(annotation: Any, *constraints: Any) -> TypeAdapter[Any]:
    """An adapter for a type built at runtime, with pydantic constraints on it."""
    if constraints:
        annotation = Annotated[annotation, *constraints]
    return TypeAdapter(annotation)


def _literal(nicks: tuple[str, ...]) -> Any:
    return Literal[nicks]


def _members(gclass: Any, values: str) -> Iterable[Any]:
    """The members of a GEnum or GFlags class. PyGObject 3.50 made them Python enums and
    deprecated their `values` dict (`__enum_values__`, `__flags_values__`); 3.48, which
    Ubuntu 24.04 ships, has only that dict."""
    return gclass if isinstance(gclass, enum.EnumType) else getattr(gclass, values).values()


def _make_codec(ps: Any) -> Codec | None:
    """The codec of a ParamSpec, None when its type is left out of the model."""
    vt = ps.value_type
    same: Callable[[Any], Any] = lambda value: value  # noqa: E731
    if vt == GObject.TYPE_BOOLEAN:
        return Codec(_adapter(bool), same, same, ps.default_value)
    if vt in _INTS or vt in _FLOATS:
        number = int if vt in _INTS else float
        return Codec(
            _adapter(number, Field(ge=ps.minimum, le=ps.maximum)), same, same, ps.default_value
        )
    if vt == GObject.TYPE_STRING:
        return Codec(_adapter(str | None), same, same, ps.default_value)
    if GObject.type_is_a(vt, GObject.TYPE_ENUM):
        enum_class = ps.enum_class  # kept alive by `nick`: PyGObject drops unreferenced ones
        nicks = tuple(member.value_nick for member in _members(enum_class, "__enum_values__"))

        def nick(value: Any) -> str:
            return str(enum_class(value).value_nick)

        return Codec(_adapter(_literal(nicks)), nick, same, nick(ps.default_value))
    if GObject.type_is_a(vt, GObject.TYPE_FLAGS):
        flags_class = ps.flags_class  # kept alive by `combine`: PyGObject drops unreferenced ones
        zero = flags_class(0).value_nicks  # the "none" every value lists
        bits: dict[str, int] = {}
        for member in _members(flags_class, "__flags_values__"):
            if len(names := [n for n in member.value_nicks if n not in zero]) == 1:
                bits[names[0]] = int(member)
        if not bits:
            return None

        def nicks_of(value: Any) -> list[str]:
            return [nick for nick, bit in bits.items() if int(value) & bit == bit]

        def split(value: Any) -> Any:
            return [nick for nick in value.split("+") if nick] if isinstance(value, str) else value

        def combine(selected: list[str]) -> Any:
            return flags_class(functools.reduce(operator.or_, (bits[nick] for nick in selected), 0))

        items = types.GenericAlias(list, _literal(tuple(bits)))
        adapter = _adapter(items, BeforeValidator(split))
        return Codec(adapter, nicks_of, combine, nicks_of(ps.default_value))
    # caps and structures travel as strings; validation returns the parsed object
    for gtype, parse, kind in (
        (Gst.Caps.__gtype__, _parse_caps, "gst-caps"),
        (Gst.Structure.__gtype__, _parse_structure, "gst-structure"),
    ):
        if vt == gtype:
            schema = Field(json_schema_extra={"format": kind})
            # None is what an unset caps reads as (appsrc), and clears it
            adapter = _adapter(str | None, schema, AfterValidator(_unless_none(parse)))
            return Codec(adapter, _to_string, same)
    return None


_codecs: dict[tuple[str, str], Codec | None] = {}


def codec_for(ps: Any) -> Codec | None:
    """The codec for a property, None when the model leaves it out (objects, `name`, ...)."""
    if ps.owner_type.name == "GstObject":  # name and parent
        return None
    key = (ps.owner_type.name, ps.name)
    if key not in _codecs:
        _codecs[key] = _make_codec(ps)
    return _codecs[key]


def exposed_props(element: Gst.Element) -> Iterator[tuple[Any, Codec]]:
    """The ParamSpecs of an element's properties the model supports, with their codecs."""
    for ps in GObject.list_properties(element):
        codec = codec_for(ps)
        if codec is not None:
            yield ps, codec


def factory_name(element: Gst.Element) -> str:
    factory = element.get_factory()
    return str(factory.get_name()) if factory else str(element.__gtype__.name)


def is_named(element: Gst.Element) -> bool:
    """False for the `<factory><N>` names GStreamer gives elements without `name=`."""
    return re.fullmatch(re.escape(factory_name(element)) + r"\d+", element.get_name()) is None


def is_user_named(element: Gst.Element) -> bool:
    """Named with `name=` by the user rather than by an element for its own children:
    `is_named`, and inside plain bins all the way up to the pipeline, not in playbin or
    decodebin (whose `playsink` has no number), nor in the plain bins they make for
    themselves (playsink's `vbin`). The pipeline itself has no parent and counts."""
    if not is_named(element):
        return False
    parent = element.get_parent()
    while parent is not None:
        if parent.__gtype__ not in (Gst.Bin.__gtype__, Gst.Pipeline.__gtype__):
            return False
        parent = parent.get_parent()
    return True


def element_prop_spec(element: Gst.Element, key: str, ps: Any, codec: Codec) -> PropSpec:
    """The spec of a property of `element`, known as `key` (see `Player._element_keys`)."""
    schema = codec.adapter.json_schema()
    if codec.default is not None:
        schema["default"] = codec.default
    if ps.blurb:
        schema["description"] = ps.blurb
    flags = int(ps.flags)
    return PropSpec(
        path=f"{key}.{ps.name}",
        element=key,
        property=ps.name,
        schema=schema,
        readable=bool(flags & GObject.ParamFlags.READABLE),
        writable=bool(flags & GObject.ParamFlags.WRITABLE)
        and not flags & GObject.ParamFlags.CONSTRUCT_ONLY,
        controllable=bool(flags & Gst.PARAM_CONTROLLABLE),
        mutable=next((state for state, bit in _MUTABLE if flags & bit), None),
        description=ps.blurb or None,
    )


def describe_element(element: Gst.Element, key: str) -> ElementSpec:
    factory = element.get_factory()
    return ElementSpec(
        name=key,
        factory=factory_name(element),
        description=factory.get_metadata("long-name") if factory else None,
        properties={
            ps.name: element_prop_spec(element, key, ps, codec)
            for ps, codec in exposed_props(element)
        },
    )


def _builtin_prop(path: str, annotation: Any, description: str) -> PropSpec:
    schema = {**_adapter(annotation).json_schema(), "description": description}
    return PropSpec(path=path, schema=schema, writable=False, description=description)


BUILTIN_PROPS = {
    prop.path: prop
    for prop in (
        _builtin_prop("state", _literal(("null", "ready", "paused", "playing")), "Pipeline state"),
        _builtin_prop("position", float | None, "Playback position in seconds"),
        _builtin_prop("duration", float | None, "Duration in seconds"),
    )
}


# -- declarations -- #


class Prop:
    """A class attribute declaring an alias of an element property, see `prop()`."""

    name: str

    def __init__(self, target: str, label: str | None, description: str | None) -> None:
        self.target, self.label, self.description = target, label, description

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: Any, owner: type) -> Any: ...
    def __get__(self, instance: Any, owner: type) -> Any:
        return self if instance is None else instance.get(self.name)

    def __set__(self, instance: Any, value: Any) -> None:
        raise AttributeError(f"Use `await player.set({self.name!r}, value)` to change it")

    def spec(self, target: PropSpec) -> PropSpec:
        """The alias's spec: its target's, under the alias name."""
        schema = dict(target.value_schema)
        if self.label:
            schema["title"] = self.label
        return target.model_copy(
            update={
                "path": self.name,
                "target": target.path,
                "value_schema": schema,
                "description": self.description or target.description,
            }
        )


def prop(target: str, *, label: str | None = None, description: str | None = None) -> Any:
    """Declare an alias of the element property `target` (`"enc.bitrate"`) on a `Player`."""
    return Prop(target, label, description)


_COMMAND = "__mixtape_command__"


def command[F: Callable[..., Any]](method: F) -> F:
    """Mark a sync or async method of a `Player` as a command."""
    setattr(method, _COMMAND, True)
    return method


@dataclass(frozen=True)
class Command:
    spec: CommandSpec
    args: type[BaseModel]
    """Validates the arguments"""


def _inline_defs(schema: dict[str, Any], what: str) -> dict[str, Any]:
    """The schema with every `#/$defs/X` reference replaced by the definition, so it can be
    embedded elsewhere (OpenAPI) where those references would dangle. Keys next to a
    `$ref` (default, description) are merged over the definition. A recursive model
    cannot be inlined: it raises TypeError."""
    defs: dict[str, Any] = schema.get("$defs", {})

    def resolve(node: Any, active: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [resolve(item, active) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            key = ref.removeprefix("#/$defs/")
            if key in active:
                raise TypeError(f"{what}: the recursive model {key!r} has no finite JSON Schema")
            inlined = resolve(defs[key], (*active, key))
            return inlined | {k: resolve(v, active) for k, v in node.items() if k != "$ref"}
        return {k: resolve(v, active) for k, v in node.items() if k != "$defs"}

    resolved: dict[str, Any] = resolve(schema, ())
    return resolved


def _build_command(method: Callable[..., Any], name: str, builtin: bool) -> Command:
    hints = typing.get_type_hints(method, include_extras=True)
    fields: dict[str, Any] = {}
    for param in list(inspect.signature(method).parameters.values())[1:]:  # after self
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise TypeError(f"{method.__qualname__}: commands take no *args or **kwargs")
        if param.name not in hints:
            raise TypeError(f"{method.__qualname__}: parameter {param.name!r} needs a type hint")
        default = ... if param.default is param.empty else param.default
        fields[param.name] = (hints[param.name], default)
    args = create_model(name, __config__=ConfigDict(extra="forbid"), **fields)
    returns = hints.get("return", type(None))
    what = method.__qualname__
    result = None
    # built-ins return Gst values (StateChangeReturn) that are no interface's business
    if not builtin and returns is not type(None):
        result = _inline_defs(TypeAdapter(returns).json_schema(), f"{what} result")
    spec = CommandSpec(
        name=name,
        description=(inspect.getdoc(method) or "").partition("\n")[0] or None,
        parameters=_inline_defs(args.model_json_schema(), f"{what} parameters"),
        result=result,
        builtin=builtin,
    )
    return Command(spec, args)


class Declarations(NamedTuple):
    props: dict[str, Prop]
    commands: dict[str, Command]


def collect(cls: type, root: type) -> Declarations:
    """The `prop` and `@command` declarations of `cls` and its bases; `root`'s are built-in."""
    props: dict[str, Prop] = {}
    commands: dict[str, Command] = {}
    for klass in reversed(cls.__mro__):
        for name, attr in vars(klass).items():
            if isinstance(attr, Prop):
                if name in BUILTIN_PROPS:
                    raise TypeError(f"{klass.__qualname__}.{name} clashes with a built-in property")
                props[name] = attr
            elif getattr(attr, _COMMAND, False):
                commands[name] = _build_command(attr, name, klass is root)
    return Declarations(props, commands)
