"""Features: plugins a player enables to add commands and live properties to its model.

A `Feature` subclass declares `@command` methods and `@prop` getters like a `Player` does;
they show up in `describe()`, `mixtape inspect`, the HTTP API and the control page as
`feature.name`. Its `__init__` keyword parameters are its options. Features are found by name
in the `mixtape.features` entry point group, and enabled with `--feature NAME[,KEY=VALUE…]`,
`Player.features` or the `features` argument of the player.
"""

import importlib.metadata
import re
import typing
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel, ValidationError

from ..exceptions import FeatureError
from ..model import Declarations, args_model, collect, validation_reasons

if TYPE_CHECKING:
    from ..player import Player

__all__ = ["Feature", "FeatureError", "FeatureSpec", "available", "resolve", "resolve_all"]

GROUP = "mixtape.features"
_NAME = re.compile(r"[a-z][a-z0-9_]*")


class Feature:
    """A plugin of a `Player`. Subclass it with a `name`, the options as `__init__` keyword
    parameters, and `@command` methods and `@prop` getters for its model.

    Getters and commands must work when the player is not set up (`mixtape inspect` reads
    every property), so a feature that needs the loop answers from what it has.
    """

    name: ClassVar[str]
    """Its name in the model (`name.command`) and the entry point; `[a-z][a-z0-9_]*`"""
    requires: ClassVar[tuple[str, ...]] = ()
    """GStreamer plugin features (elements or tracers) the player needs to enable it"""

    player: "Player | None" = None
    """The player it is attached to: set just before `attach`, None again after `detach`"""

    _options: ClassVar[type[BaseModel]]
    _declared: ClassVar[Declarations]
    _prepared = False

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
        (`"0.5"` is a float). Raises `FeatureError` for a rejected one."""
        try:
            options = cls._options.model_validate(raw)
        except ValidationError as exc:
            raise FeatureError(
                f"Invalid options for feature {cls.name!r}: {validation_reasons(exc)}"
            ) from exc
        return cls(**{key: getattr(options, key) for key in type(options).model_fields})

    def prepare(self) -> None:
        """Called before GStreamer is initialised, when the player builds the pipeline from a
        description: the place to set environment variables such as `GST_TRACERS`."""

    def check(self, player: "Player") -> None:
        """Called when the player is built, after GStreamer is initialised and `requires` is
        met, before it is set up: raise `FeatureError` if the feature cannot work here, so the
        player is not built rather than failing later in `attach`."""

    def attach(self, player: "Player") -> None:
        """Called by `Player.setup`, on the running loop. One that raises gets no `detach`:
        it cleans up after itself, and the features attached before it are detached."""

    def detach(self, player: "Player") -> None:
        """Called by `Player.teardown`, once the pipeline is in NULL. An exception is logged
        and teardown goes on."""

    def notify(self, prop: str) -> None:
        """Publish the current value of its computed property `prop` to the player's event
        streams. Does nothing while detached. On the loop thread only."""
        if self.player is not None:
            self.player.notify(f"{self.name}.{prop}")

    def _prepare_once(self) -> None:
        if not self._prepared:
            self._prepared = True
            self.prepare()


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
    only inside parentheses), looked up in
    `available()`; a class is built with its default options; an instance is itself."""
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
    installed = available()
    if name not in installed:
        names = ", ".join(sorted(installed)) or "none installed"
        raise FeatureError(f"Unknown feature {name!r}; available: {names}")
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
