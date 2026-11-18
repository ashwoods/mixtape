"""Features: plugins a player enables to add commands and live properties to its model.

A `Feature` subclass declares `@command` methods and `@prop` getters like a `Player` does;
they show up in `describe()`, `mixtape inspect`, the HTTP API and the control page as
`feature.name`. Its `__init__` keyword parameters are its options. Features are found by name
in the `mixtape.features` entry point group, and enabled with `--feature NAME[,KEY=VALUE…]`,
`Player.features` or the `features` argument of the player.
"""

import asyncio
import importlib.metadata
import logging
import re
import typing
from collections.abc import Callable, Coroutine, Iterable
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from pydantic import BaseModel, ValidationError

from ..exceptions import FeatureError
from ..model import Declarations, args_model, collect, validation_reasons

if TYPE_CHECKING:
    from ..player import Player

__all__ = ["Feature", "FeatureError", "FeatureSpec", "available", "resolve", "resolve_all"]

logger = logging.getLogger(__name__)  # `mixtape.features`: where a feature's failed task is logged

GROUP = "mixtape.features"
GST = "gst:"
_NAME = re.compile(r"[a-z][a-z0-9_]*")


class Feature:
    """A plugin of a [`Player`][mixtape.Player]. Subclass it with a `name`, the options as
    `__init__` keyword parameters, and `@command` methods and `@prop` getters for its model.

    Getters and commands must work when the player is not set up (`mixtape inspect` reads
    every property), so a feature that needs the loop answers from what it has.
    """

    name: str
    """Its name in the model (`name.command`) and the entry point; `[a-z][a-z0-9_]*`"""
    requires: ClassVar[tuple[str, ...]] = ()
    """GStreamer plugin features (elements or tracers) the player needs to enable it"""
    own_tracers: ClassVar[tuple[str, ...]] = ()
    """GStreamer tracers it reads itself: `gst:NAME` of one is refused beside it, as both
    would use the one tracer of the process"""

    player: "Player | None" = None
    """The player it is attached to: set just before [`attach`][mixtape.Feature.attach], None
    again after [`detach`][mixtape.Feature.detach]"""

    _options: ClassVar[type[BaseModel]]
    _declared: Declarations  # the class's; `gst:` features have their own
    _prepared = False
    _initialised = False
    _tasks: set[asyncio.Task[Any]]  # `spawn`ed, until they end; the player sets it on attach

    def __init__(self) -> None:
        pass

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise TypeError(f"{cls.__qualname__}.name must match {_NAME.pattern}")
        hints = typing.get_type_hints(cls.__init__, include_extras=True)
        cls._options = args_model(cls.__init__, f"{cls.__name__}Options", hints, "options")
        cls._declared = collect(cls, Feature)  # a bad declaration fails when the class is defined
        if cls._declared.props:
            raise TypeError(f"{cls.__qualname__}: features declare computed properties only")

    @classmethod
    def from_options(cls, **raw: Any) -> Self:
        """The feature with its options validated, strings from a command line included
        (`"0.5"` is a float). Raises [`FeatureError`][mixtape.exceptions.FeatureError] for a
        rejected one."""
        try:
            options = cls._options.model_validate(raw)
        except ValidationError as exc:
            for error in exc.errors(include_url=False, include_context=False):
                if error["type"] == "extra_forbidden":
                    raise FeatureError(
                        f"Feature {cls.name!r} has no option {error['loc'][0]!r}; "
                        f"its options: {cls._option_list() or 'none'}"
                    ) from exc
            raise FeatureError(
                f"Invalid options for feature {cls.name!r}: {validation_reasons(exc)}"
            ) from exc
        return cls(**{key: getattr(options, key) for key in type(options).model_fields})

    @classmethod
    def _option_list(cls) -> str:
        """The options with a short type, as `out (path), interval (number > 0)`."""
        properties = cls._options.model_json_schema()["properties"]
        return ", ".join(f"{name} ({_option_type(schema)})" for name, schema in properties.items())

    def prepare(self) -> None:
        """Called before GStreamer is initialised, when the player builds the pipeline from a
        description: the place to set environment variables such as `GST_TRACERS`."""

    def initialised(self) -> None:
        """Called once GStreamer is initialised (by the player or before), and before the player
        builds its pipeline from a description: the place for what has to be in place before an
        element is made, such as a logger. Raise
        [`FeatureError`][mixtape.exceptions.FeatureError] if the feature cannot work."""

    def check(self, player: "Player") -> None:
        """Called when the player is built, after GStreamer is initialised and
        [`requires`][mixtape.Feature.requires] is met, before it is set up: raise
        [`FeatureError`][mixtape.exceptions.FeatureError] if the feature cannot work here, so the
        player is not built rather than failing later in `attach`."""

    def attach(self, player: "Player") -> None:
        """Called by [`Player.setup`][mixtape.Player.setup], on the running loop. One that
        raises gets no `detach`: it cleans up after itself, and the features attached before
        it are detached."""

    def detach(self, player: "Player") -> None:
        """Called by [`Player.teardown`][mixtape.Player.teardown], once the pipeline is in NULL.
        An exception is logged and teardown goes on."""

    def notify(self, prop: str) -> None:
        """Publish the current value of its computed property `prop` to the player's event
        streams. Does nothing while detached. On the loop thread only."""
        if self.player is not None:
            self.player.notify(f"{self.name}.{prop}")

    def call_soon(self, callback: Callable[..., object], *args: Any) -> None:
        """Run `callback(*args)` on the player's loop, from a signal handler on any thread.
        Does nothing while detached, or when the loop is closing: a handler that races the
        teardown needs no check of its own."""
        player = self.player  # read once: the teardown clears it on the loop thread
        if player is not None:
            player._call_soon(callback, *args)

    def spawn[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Start `coro` as a task on the player's loop. Call it from
        [`attach`][mixtape.Feature.attach] or later, on the loop thread.

        The host supervises the task: one that ends with an exception (not a cancellation) is
        logged with its traceback on the `mixtape.features` logger, naming the feature, and
        the player cancels the ones still running right after
        [`detach`][mixtape.Feature.detach] returns. Raises
        [`FeatureError`][mixtape.exceptions.FeatureError] while detached: there is no loop
        to run it on, and nothing would cancel it."""
        if self.player is None:
            coro.close()  # never awaited: no warning for it
            raise FeatureError(f"The feature {self.name!r} can spawn a task only while attached")
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("Task of feature %r failed", self.name, exc_info=exc)

    def _end_tasks(self, *, cancel: bool) -> None:
        """Forget the tasks it spawned, and cancel them unless their loop is gone."""
        tasks, self._tasks = self._tasks, set()
        if cancel:
            for task in tasks:
                task.cancel()

    def _command(self, name: str) -> Callable[..., Any]:
        """What its command `name` calls."""
        return cast(Callable[..., Any], getattr(self, name))

    def _prepare_once(self) -> None:
        if not self._prepared:
            self._prepared = True
            self.prepare()

    def _initialised_once(self) -> None:
        if not self._initialised:
            self._initialised = True
            self.initialised()


def _option_type(schema: dict[str, Any]) -> str:
    """A short type of an option from its JSON Schema: `path`, `number > 0`, `integer >= 1`."""
    if "anyOf" in schema:  # an optional value: its type, without the null
        return " or ".join(_option_type(s) for s in schema["anyOf"] if s.get("type") != "null")
    if schema.get("format") == "path":
        return "path"
    if "enum" in schema:
        return "|".join(map(str, schema["enum"]))
    kind = str(schema.get("type", "any"))
    limits = {"exclusiveMinimum": ">", "minimum": ">=", "exclusiveMaximum": "<", "maximum": "<="}
    bounds = [f"{sign} {schema[key]:g}" for key, sign in limits.items() if key in schema]
    return " ".join([kind, *bounds])


type FeatureSpec = str | type[Feature] | Feature
"""What enables a feature: `name[,key=value…]`, its class or an instance"""


def available() -> dict[str, importlib.metadata.EntryPoint]:
    """The features installed, by name: the entry points of the `mixtape.features` group."""
    return {ep.name: ep for ep in importlib.metadata.entry_points(group=GROUP)}


def _name_of(spec: FeatureSpec) -> str:
    """The feature name a spec stands for, without loading or building anything."""
    if isinstance(spec, str):
        return spec.partition(",")[0]
    return spec.name


def _split_options(text: str) -> list[str]:
    """The `key=value` items of a spec, split at the commas outside parentheses, so a value
    such as `latency(flags=element,filter=x)` keeps its own."""
    items: list[str] = []
    depth = start = 0
    for i, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == "," and not depth:
            items.append(text[start:i])
            start = i + 1
    return [*items, text[start:]]


def resolve(spec: FeatureSpec) -> Feature:
    """The feature a spec stands for. A string is `name[,key=value…]` (a value has commas
    only inside parentheses), looked up in `available()`, or `gst:TRACER[,key=value…]`, any
    GStreamer tracer (`mixtape.features.gst`); a class is built with its default options; an
    instance is itself."""
    if isinstance(spec, Feature):
        return spec
    if not isinstance(spec, str):
        return spec.from_options()
    name, _, rest = spec.partition(",")
    options: dict[str, str] = {}
    for item in _split_options(rest) if rest else []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise FeatureError(f"Invalid option {item!r} of feature {name!r}: not KEY=VALUE")
        options[key] = value
    if name.startswith(GST):
        from .gst import GstTracer  # it imports this module

        return GstTracer(name.removeprefix(GST), options)
    installed = available()
    if name not in installed:
        names = ", ".join(sorted(installed)) or "none installed"
        raise FeatureError(
            f"Unknown feature {name!r}; available: {names}, or {GST}NAME for any GStreamer tracer"
        )
    cls: Any = installed[name].load()
    if not (isinstance(cls, type) and issubclass(cls, Feature)) or cls.name != name:
        raise FeatureError(f"The entry point {name!r} is not a Feature subclass named {name!r}")
    return cls.from_options(**options)


def resolve_all(
    explicit: Iterable[FeatureSpec], defaults: Iterable[str | type[Feature]]
) -> dict[str, Feature]:
    """The features of a player by name: the `explicit` ones, then those of `defaults` (the
    class attribute `Player.features`) whose name is not taken. Two explicit ones of one name
    raise `FeatureError`."""
    features: dict[str, Feature] = {}
    for spec in explicit:
        feature = resolve(spec)
        if feature.name in features:
            raise FeatureError(f"The feature {feature.name!r} is enabled twice")
        features[feature.name] = feature
    for spec in defaults:
        if _name_of(spec) not in features:
            feature = resolve(spec)
            features[feature.name] = feature
    return features
