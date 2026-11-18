"""One subclassable asyncio player for a GStreamer pipeline."""

import asyncio
import concurrent.futures
import contextlib
import inspect
import logging
import warnings
from collections import Counter
from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any, ClassVar, Literal, NamedTuple, Self

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst
from pydantic import NonNegativeFloat, ValidationError

from .. import events, hints
from ..exceptions import (
    CommandError,
    FeatureError,
    InvalidValueError,
    MixTapeError,
    NotReadableError,
    NotWritableError,
    PlayerDescriptionError,
    PlayerNotConfigured,
    PlayerPipelineError,
    PlayerSetStateError,
    UnknownNameError,
)
from ..features import Feature, FeatureSpec, resolve_all
from ..model import (
    BUILTIN_PROPS,
    Codec,
    Command,
    Computed,
    Declarations,
    action_signals,
    codec_for,
    collect,
    command,
    factory_name,
    describe_element,
    element_prop_spec,
    exposed_props,
    is_user_named,
    validation_reasons,
)
from ..spec import GraphSpec, LinkSpec, NodeSpec, PlayerSpec
from .state import StateMachine
from .util import _retrying

logger = logging.getLogger(__name__)

MessageCallback = Callable[[Gst.Message], None]


def ensure_init() -> None:
    """Initialise GStreamer, once."""
    if not Gst.is_initialized():
        Gst.init(None)


def _description_elements(bin: Gst.Bin) -> Iterator[Gst.Element]:
    """The elements of `bin` and, deep, of its plain bins: those a description can make."""
    plain = (Gst.Bin.__gtype__, Gst.Pipeline.__gtype__)
    for element in _retrying(bin.iterate_elements):
        yield element
        if isinstance(element, Gst.Bin) and element.__gtype__ in plain:
            yield from _description_elements(element)


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


def _invalid(what: str, exc: ValidationError) -> InvalidValueError:
    """An InvalidValueError with pydantic's errors on one line, for a CLI or an HTTP detail."""
    errors = exc.errors(include_url=False, include_context=False)
    return InvalidValueError(f"Invalid {what}: {validation_reasons(exc)}", errors)


class _Bound(NamedTuple):
    """A command of one player: the declaration, and what it calls with the arguments."""

    declared: Command
    call: Callable[..., Any]
    owner: Any = None
    """What its check is called with: the player, or the feature the command belongs to"""


class Player:
    """Drive a `Gst.Pipeline` from an asyncio loop.

    The player reads the pipeline's bus on the loop it is attached to,
    which happens lazily on the first `await` of any of its methods.
    Subclass it and override the state methods, or react to bus messages
    with [`add_handler`][mixtape.Player.add_handler] and [`events()`][mixtape.Player.events].

    Its controls and state are described by [`describe()`][mixtape.Player.describe]: the
    properties of the named elements (all of them with `expose_all`), the aliases declared with
    [`prop()`][mixtape.prop], the computed properties declared with `@prop`, the commands
    declared with [`@command`][mixtape.command], and the built-ins. The features it enables
    add their own, as `feature.name`.
    """

    features: ClassVar[Sequence[str | type[Feature]]] = ()
    """The features every instance enables, as `"name,key=value"` strings or classes: never
    instances, which would be shared. The `features` argument adds to them and wins by name."""
    description: ClassVar[str | None] = None
    """The pipeline description of a subclass that carries its own, for `mixtape run`"""
    position_interval: ClassVar[float] = 0.25
    """Seconds between the reads that publish [`Position`][mixtape.events.Position] to
    `events(position=True)` streams"""
    state_timeout: ClassVar[float] = 5.0
    """Seconds a change down to READY or NULL may take: a streaming thread that hangs blocks
    it, and the player then calls the pipeline stuck until the change finishes after all"""
    shape_interval: ClassVar[float] = 0.1
    """Seconds the player gathers changes to the pipeline's shape (elements, pads, links, caps)
    before it publishes them as one [`GraphChanged`][mixtape.events.GraphChanged], and
    [`ModelChanged`][mixtape.events.ModelChanged] when they change it"""

    _declared: ClassVar[Declarations]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._declarations()  # a bad declaration fails when the class is defined
        if any(isinstance(spec, Feature) for spec in cls.features):
            raise TypeError(f"{cls.__qualname__}.features takes names and classes, not instances")

    @classmethod
    def _declarations(cls) -> Declarations:
        if "_declared" not in cls.__dict__:
            cls._declared = collect(cls, Player)
        return cls._declared

    def __init__(
        self,
        pipeline: Gst.Pipeline,
        *,
        expose_all: bool = False,
        features: Iterable[FeatureSpec] = (),
    ) -> None:
        self._features = resolve_all(features, type(self).features)
        for feature in self._features.values():
            feature._prepare_once()  # no-op when `from_description` did
        ensure_init()
        for feature in self._features.values():
            feature._initialised_once()  # no-op when `from_description` did
        self._description: str | None = None  # the one `from_description` built it from
        if not isinstance(pipeline, Gst.Pipeline):
            raise TypeError(f"pipeline must be a Gst.Pipeline, not {type(pipeline).__name__}")
        self.pipeline = pipeline
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fd: int | None = None
        self._machine = StateMachine(self)  # the only owner of the pipeline's state
        self._handlers: dict[object, tuple[Gst.MessageType, MessageCallback]] = {}
        self._events = events.Broadcast()
        self._position_task: asyncio.Task[None] | None = None  # while a position stream is open
        self._last_position: tuple[float | None, float | None] | None = None  # the last published
        self._expose_all = expose_all
        self._notify_ids: list[tuple[Gst.Element, int]] = []
        self._watched: dict[Gst.Element, str] = {}  # the key its notify handlers publish
        self._shape_ids: dict[Gst.Object, list[int]] = {}  # element and pad handlers
        self._shape_flush: asyncio.TimerHandle | None = None  # a publication on its way
        self._commands_flush: asyncio.Handle | None = None  # a check of the commands on its way
        self._commands_seen: dict[str, str] = {}  # `unavailable()` as last published or seen
        self._unchecked: set[str] = set()  # the commands whose failing check was logged
        self._position_failed = False  # a failing poll of the position was logged
        self._graph_seen: GraphSpec | None = None  # as last published
        self._exposed_seen: list[str] = []  # the keys of the exposed elements, likewise
        self._bin_ids: list[int] = []  # the pipeline's deep-element-added/removed handlers
        self._relink_ids: list[tuple[Gst.Element, int]] = []  # pad-added, to link again
        self._relinks: dict[Gst.Element, list[Gst.Pad]] = {}  # the peers its new pads had
        self._attached: list[Feature] = []  # in the order they attached
        for alias in self._declarations().props.values():
            self._locate(alias.target, exposed=False)  # a typo fails here
        self._commands, self._computed = self._build_model()
        self._check_features()  # after the model: a feature's `check` may read it

    @classmethod
    def from_description(
        cls,
        description: str | None = None,
        *,
        expose_all: bool = False,
        features: Iterable[FeatureSpec] = (),
    ) -> Self:
        """Build a player from a `gst-launch` style pipeline description, by default the
        class's own `description`.

        The features are prepared first: before GStreamer is initialised, so they can set
        its environment."""
        if description is None:
            if cls.description is None:
                raise PlayerDescriptionError(
                    f"{cls.__name__} has no description: pass one to from_description"
                )
            description = cls.description
        resolved = list(resolve_all(features, cls.features).values())
        for feature in resolved:
            feature._prepare_once()
        ensure_init()
        for feature in resolved:
            feature._initialised_once()
        try:
            element = Gst.parse_launch(description)
        except GLib.Error as exc:
            raise PlayerDescriptionError(
                f"Invalid pipeline {description!r}: {exc.message}"
            ) from exc
        if not isinstance(element, Gst.Pipeline):
            pipeline = Gst.Pipeline.new(None)
            pipeline.add(element)
            element = pipeline
        if not resolved:  # a subclass whose `__init__` predates features still works
            player = cls(element, expose_all=expose_all)
        else:
            player = cls(element, expose_all=expose_all, features=resolved)
        player._description = description
        return player

    @classmethod
    def run(cls, description_or_pipeline: str | Gst.Pipeline, timeout: float | None = None) -> None:
        """Play a pipeline until EOS and tear it down, for scripts.

        Raises [`PlayerPipelineError`][mixtape.exceptions.PlayerPipelineError] on a pipeline
        error and `TimeoutError`
        when `timeout` seconds pass first.
        """
        if isinstance(description_or_pipeline, str):
            player = cls.from_description(description_or_pipeline)
        else:
            player = cls(description_or_pipeline)

        async def main() -> None:
            async with player:
                await asyncio.wait_for(player.play_until_eos(), timeout)

        asyncio.run(main())

    # -- inspection -- #

    @property
    def pipeline_description(self) -> str | None:
        """The description the player was built from by
        [`from_description`][mixtape.Player.from_description], None for one built from a
        pipeline"""
        return self._description

    @property
    def bus(self) -> Gst.Bus:
        """The pipeline's bus"""
        return self.pipeline.get_bus()

    @property
    def state(self) -> Gst.State:
        """The current pipeline state, asked from the pipeline"""
        return self.pipeline.get_state(0)[1]

    @property
    def pending(self) -> Gst.State:
        """The pending pipeline state, `VOID_PENDING` when not changing"""
        return self.pipeline.get_state(0)[2]

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The asyncio loop the player is attached to"""
        return self._loop

    @property
    def error(self) -> PlayerPipelineError | None:
        """The first error since the last change to another state than NULL, which stopped
        the pipeline, until the next such change"""
        return self._machine.error

    @property
    def enabled_features(self) -> Mapping[str, Feature]:
        """The features of this player by name, in the order they were enabled"""
        return MappingProxyType(self._features)

    @property
    def sinks(self) -> list[Gst.Element]:
        """Returns all sink elements"""
        return list(self.pipeline.iterate_sinks())

    @property
    def sources(self) -> list[Gst.Element]:
        """Return all source elements"""
        return list(self.pipeline.iterate_sources())

    @property
    def elements(self) -> list[Gst.Element]:
        """Return all pipeline elements"""
        return list(self.pipeline.iterate_elements())

    def get_elements_by_gtype(self, gtype: Any) -> list[Gst.Element]:
        """Return all elements in pipeline that match gtype"""
        return [e for e in self.elements if e.get_factory().get_element_type() == gtype]

    # -- attaching to a loop -- #

    def setup(self) -> None:
        """Attach to the running loop. Idempotent; every async method calls it."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise PlayerNotConfigured("setup needs a running asyncio loop") from exc
        if self._loop is not None and self._loop.is_closed():
            self._detach_features()  # they were attached to it
            self._loop = self._fd = None  # the loop died without a teardown
            self._events = events.Broadcast()  # and its subscribers with it
            self._position_task = self._last_position = None
            self._commands_flush = None
            self._machine.forget()  # their futures died with it
            self._disconnect_relinks()  # connected again below
        if self._loop is loop:
            return
        if self._loop is not None:
            raise PlayerNotConfigured("The player is attached to a different loop")
        fd = self.bus.get_pollfd().fd
        loop.add_reader(fd, self._drain)
        self._loop, self._fd = loop, fd
        self._connect_notify()
        self._connect_relinks()
        for feature in self._features.values():
            feature.player = self
            feature._tasks = set()  # its own, even in a copy of a feature
            try:
                feature.attach(self)
            except BaseException:
                feature._end_tasks(cancel=True)  # what it spawned before it failed
                feature.player = None
                self.teardown()  # detaches those before it
                raise
            self._attached.append(feature)
        self._commands_seen = self.unavailable()  # the first change after this is news

    def _detach_features(self) -> None:
        """Detach the attached features, the last first. A failing one is logged, not raised."""
        attached, self._attached = self._attached, []
        loop = self._loop
        cancellable = loop is not None and not loop.is_closed()  # else the tasks died with it
        for feature in reversed(attached):
            try:
                feature.detach(self)
            except Exception:
                logger.exception("Feature %r failed to detach", feature.name)
            finally:
                feature._end_tasks(cancel=cancellable)
                feature.player = None

    def teardown(self) -> None:
        """Set the pipeline to NULL and detach from the loop. Idempotent, re-usable after.

        A pipeline that fails to stop, or does not within `state_timeout`, is left as it is
        with a warning, and kept on purpose for the life of the process: GStreamer cannot
        free an element outside NULL. The features detach once it is in NULL, so they see
        what it left."""
        self._teardown(null=True)

    def _teardown(self, *, null: bool) -> None:
        self._disconnect_notify()
        self._disconnect_relinks()
        self._machine.teardown_null(null=null)
        self._detach_features()
        self._machine.reset()
        loop, fd = self._loop, self._fd
        self._loop = self._fd = None
        if self._position_task is not None and loop is not None and not loop.is_closed():
            self._position_task.cancel()
        self._position_task = self._last_position = None
        if self._commands_flush is not None:
            self._commands_flush.cancel()
            self._commands_flush = None
        waiters = self._machine.take_waiters()
        if loop is not None and not loop.is_closed():
            if fd is not None:
                loop.remove_reader(fd)
            self._machine.end_waits(waiters)
            self._events.close()
        else:
            self._events = events.Broadcast()  # its subscribers died with the loop
        self._machine.clear_reports()

    async def __aenter__(self) -> Self:
        self.setup()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, *rest: object) -> None:
        """Stop the pipeline and tear down. A stop that fails or hangs is raised, unless the
        block raised something else than a `PlayerSetStateError`: it is logged then."""
        if self._loop is None:
            return  # torn down in the block already
        error: MixTapeError | None = None
        try:
            await self.set_state(Gst.State.NULL)
        except MixTapeError as exc:
            error = exc
        finally:
            self._teardown(null=error is None)
        if error is None:
            return
        if exc_type is None or issubclass(exc_type, PlayerSetStateError):
            raise error
        logger.warning("%s", error)

    def call_soon_threadsafe(self, callback: Callable[..., object], *args: object) -> None:
        """Run `callback(*args)` on the player's loop, from any thread."""
        self._attached_loop().call_soon_threadsafe(callback, *args)

    def run_coroutine_threadsafe[T](
        self, coro: Coroutine[Any, Any, T]
    ) -> concurrent.futures.Future[T]:
        """Run `coro` on the player's loop, from any thread."""
        return asyncio.run_coroutine_threadsafe(coro, self._attached_loop())

    def _attached_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop  # once: teardown may clear it while another thread is here
        if loop is None or loop.is_closed():
            raise PlayerNotConfigured("The player is not attached to a running loop")
        return loop

    # -- state -- #

    async def set_state(self, state: Gst.State) -> Gst.StateChangeReturn:
        """Set the pipeline state, returning once the change is complete."""
        self.setup()
        return await self._machine.set_state(state)

    async def ready(self) -> Gst.StateChangeReturn:
        return await self.set_state(Gst.State.READY)

    @command(when=lambda self: self._machine.stuck_reason() or True)
    async def pause(self) -> Gst.StateChangeReturn:
        """Pause the pipeline."""
        return await self.set_state(Gst.State.PAUSED)

    @command(when=lambda self: self._machine.stuck_reason() or True)
    async def play(self) -> Gst.StateChangeReturn:
        """Play the pipeline."""
        return await self.set_state(Gst.State.PLAYING)

    @command(when=lambda self: self._machine.stuck_reason() or True)
    async def stop(self, eos: bool = False, timeout: float | None = 5.0) -> None:
        """Set the pipeline to NULL, after an EOS when `eos` and it is playing.

        A paused pipeline cannot carry the EOS to its sinks (waiting for it blocks), and
        one at its EOS already sends no second one: both stop at once."""
        self.setup()
        if eos and self.state == Gst.State.PLAYING and not self._machine.at_eos:
            try:
                await self.send_eos(timeout)
            except TimeoutError:
                logger.warning("No EOS after %s s, stopping anyway", timeout)
            except PlayerPipelineError:
                pass  # already logged and the pipeline is already NULL
            finally:
                await self.set_state(Gst.State.NULL)
        else:
            await self.set_state(Gst.State.NULL)

    @command(when=lambda self: self._machine.eos_refusal() or True)
    async def eos(self, timeout: float | None = 5.0) -> None:
        """Send an EOS event and wait for the EOS message it causes, while playing, for up to
        `timeout` seconds: a streaming thread that hangs never carries it to the sinks."""
        self.setup()
        self._machine.raise_if_stuck()
        if (refusal := self._machine.eos_refusal()) is not None:
            raise CommandError(refusal)
        try:
            await self.send_eos(timeout)
        except TimeoutError as exc:
            raise CommandError(f"No EOS within {timeout:g} s") from exc

    @command(when=lambda self: self._machine.seek_refusal() or True)
    def seek(self, position: NonNegativeFloat) -> None:
        """Seek to a position in seconds, flushing."""
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
        if not self.pipeline.seek_simple(Gst.Format.TIME, flags, int(position * Gst.SECOND)):
            raise CommandError(f"The pipeline could not seek to {position} s")
        self._machine.flushed()  # the flush clears it: the stream goes on

    async def send_eos(self, timeout: float | None = None) -> None:
        """Send an EOS event and wait for the EOS message it causes."""
        self.setup()
        await self._machine.send_eos(timeout)

    async def play_until_eos(self) -> None:
        """Play and return on the next EOS; raise
        [`PlayerPipelineError`][mixtape.exceptions.PlayerPipelineError] on an error.

        A pipeline still at the EOS of an earlier run restarts from READY."""
        self.setup()
        if self._machine.at_eos:
            await self.ready()
        future = self._machine.eos_waiter()  # before playing, a short stream may end meanwhile
        try:
            await self.play()
            await future
        finally:
            self._machine.discard_eos_waiter(future)

    # -- the model: properties and commands -- #

    def _check_features(self) -> None:
        """Fail for a feature whose GStreamer plugin features are missing, whose name would
        shadow the `element.property` paths of an element, or that refuses the player."""
        registry = Gst.Registry.get()
        keys = self.element_keys()
        for feature in self._features.values():
            if missing := [r for r in feature.requires if registry.lookup_feature(r) is None]:
                raise FeatureError(
                    f"The feature {feature.name!r} needs the GStreamer plugin features "
                    f"{', '.join(map(repr, missing))}, which are not installed"
                )
            if feature.name in keys:
                raise FeatureError(
                    f"The feature {feature.name!r} has the name of an element: "
                    "its properties would shadow the element's"
                )
            feature.check(self)

    def _build_model(self) -> tuple[dict[str, _Bound], dict[str, tuple[Computed[Any], object]]]:
        """The commands and computed properties of this player: its class's, the action
        signals of the exposed elements as `element.signal`, then each feature's as
        `feature.name`."""

        def bound(c: Command, path: str, call: Callable[..., Any], owner: Any = None) -> _Bound:
            return _Bound(replace(c, spec=c.spec.model_copy(update={"name": path})), call, owner)

        declared = self._declarations()
        commands = {
            name: _Bound(c, getattr(self, name), self) for name, c in declared.commands.items()
        }
        for key, element in self.exposed_elements().items():
            for name, (c, emit) in action_signals(element).items():
                commands[f"{key}.{name}"] = bound(c, f"{key}.{name}", emit)
        computed: dict[str, tuple[Computed[Any], object]] = {
            name: (c, self) for name, c in declared.computed.items()
        }
        for feature in self._features.values():
            for name, c in feature._declared.commands.items():
                path = f"{feature.name}.{name}"
                commands[path] = bound(c, path, feature._command(name), feature)
            for name, p in feature._declared.computed.items():
                computed[f"{feature.name}.{name}"] = (p, feature)
        return commands, computed

    def describe(self) -> PlayerSpec:
        """A snapshot of the player's commands, properties and exposed elements."""
        properties = dict(BUILTIN_PROPS)
        for name, alias in self._declarations().props.items():
            element, ps, codec = self._locate(alias.target, exposed=False)
            key = alias.target.rpartition(".")[0]  # what `_locate` found it by
            properties[name] = alias.spec(element_prop_spec(element, key, ps, codec))
        for path, (computed, _) in self._computed.items():
            properties[path] = computed.spec(path)
        return PlayerSpec(
            name=type(self).__name__,
            features=list(self._features),
            commands={name: bound.declared.spec for name, bound in self._commands.items()},
            properties=properties,
            elements={key: describe_element(e, key) for key, e in self.exposed_elements().items()},
            unavailable=self.unavailable(),
        )

    def unavailable(self) -> dict[str, str]:
        """The commands that cannot run now, with the reason, as of this call: every command's
        [`when`][mixtape.command] check is evaluated, in the order of
        [`describe()`][mixtape.Player.describe]. Needs no running loop."""
        refused = {name: self._refusal(name, bound) for name, bound in self._commands.items()}
        return {name: reason for name, reason in refused.items() if reason is not None}

    def _refusal(self, name: str, bound: _Bound) -> str | None:
        """Why the command `name` cannot run now, else None. A check that raises, or returns
        something else than True, False or a reason, refuses it as an error."""
        when = bound.declared.when
        if when is None:
            return None
        if bound.owner is not self and bound.owner.player is None:
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
        """A snapshot of the pipeline's elements and the links between them."""
        self.setup()
        return self._graph(self.element_keys())

    def _graph(self, element_keys: dict[str, Gst.Element]) -> GraphSpec:
        keys = {e: key for key, e in element_keys.items()}
        pipeline_key = keys.pop(self.pipeline)
        nodes = {
            key: NodeSpec(
                name=e.get_name(),
                factory=factory_name(e),
                parent=keys.get(e.get_parent()),
                bin=isinstance(e, Gst.Bin),
                exposed=self._is_exposed(e),
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

    def get(self, path: str) -> Any:
        """Read a property by path: a built-in, a computed property (`feature.name`), an alias
        or `element.property`, as JSON-friendly."""
        if path in BUILTIN_PROPS:
            return self._read_builtin(path)
        if (computed := self._computed.get(path)) is not None:
            return computed[0].read(computed[1])
        element, ps, codec = self._resolve(path, "read")
        if not ps.flags & GObject.ParamFlags.READABLE:
            raise NotReadableError(f"{path!r} is write-only")
        return codec.read(element.get_property(ps.name))

    async def set(self, path: str, value: Any) -> None:
        """Validate `value` and set the property. Nothing changes when it is rejected."""
        self.setup()
        if path in BUILTIN_PROPS or path in self._computed:
            raise NotWritableError(f"{path!r} is read-only")
        element, ps, codec = self._resolve(path, "write")
        flags = int(ps.flags)
        if not flags & GObject.ParamFlags.WRITABLE or flags & GObject.ParamFlags.CONSTRUCT_ONLY:
            raise NotWritableError(f"{path!r} is read-only or can only be set on construction")
        try:
            validated = codec.adapter.validate_python(value)
        except ValidationError as exc:
            raise _invalid(f"value for {path!r}", exc) from exc
        # GLib reports an out of range value as a warning and leaves the old one
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", GObject.Warning)
            try:
                element.set_property(ps.name, codec.write(validated))
            except (TypeError, ValueError, OverflowError) as exc:
                raise InvalidValueError(f"Invalid value for {path!r}: {exc}") from exc
        self._commands_changed()  # a check may read what changed, watched or not
        if problems := [w for w in caught if issubclass(w.category, GObject.Warning)]:
            raise InvalidValueError(f"Invalid value for {path!r}: {problems[0].message}")

    async def invoke(self, name: str, /, **args: Any) -> Any:
        """Call the command `name` with validated `args` and return its result.

        Raises [`CommandError`][mixtape.exceptions.CommandError] with the reason when the
        command's [`when`][mixtape.command] check refuses it. Built-in commands return None,
        as their specs say."""
        self.setup()
        if (bound := self._commands.get(name)) is None:
            hint = hints.command_hint(self.describe().model_dump(mode="json"), name)
            raise UnknownNameError(f"Unknown command {name!r}", hint=hint)
        declared = bound.declared
        try:
            validated = declared.args.model_validate(args)
        except ValidationError as exc:
            raise _invalid(f"arguments for {name!r}", exc) from exc
        if (reason := self._refusal(name, bound)) is not None:
            raise CommandError(reason)
        try:
            result = bound.call(**{k: getattr(validated, k) for k in type(validated).model_fields})
            if inspect.isawaitable(result):
                result = await result
        finally:
            self._commands_changed()  # what it did, or failed to, may change what can run
        return None if declared.spec.builtin else result

    def element_keys(self) -> dict[str, Gst.Element]:
        """Every element of the pipeline tree by its key: its name when no other element has
        it, else the dotted names from below the pipeline (`a.q`), as in the model. Over all
        elements, the ones the model does not expose too (see
        [`exposed_elements`][mixtape.Player.exposed_elements]), so the keys do not depend on
        what is exposed. The pipeline's key is always its name, and a
        top-level child of the same name has it prefixed (`pipeline0.pipeline0`)."""

        def walk(bin: Gst.Bin) -> Iterator[Gst.Element]:
            # in the order they were added, sources first in a launch line (bins list
            # their newest child first), each bin before its children
            for child in reversed(_retrying(bin.iterate_elements)):
                yield child
                if isinstance(child, Gst.Bin):
                    yield from walk(child)

        # a streaming thread adding elements (decodebin) makes the walk RESYNC
        elements = (self.pipeline, *_retrying(lambda: walk(self.pipeline)))
        counts = Counter(e.get_name() for e in elements)
        keys: dict[str, Gst.Element] = {}
        for element in elements:
            names = [element.get_name()]
            if element is not self.pipeline and counts[names[0]] > 1:
                parent = element.get_parent()
                while parent is not None and parent != self.pipeline:
                    names.insert(0, parent.get_name())
                    parent = parent.get_parent()
                if names == [self.pipeline.get_name()]:
                    names.insert(0, names[0])
            keys[".".join(names)] = element
        return keys

    def notify(self, path: str) -> None:
        """Publish the current value of the computed property `path` (`feature.name`) as a
        `PropertyChanged` with no element. On the loop thread only: the events are not
        thread safe, `call_soon_threadsafe` brings a call from another thread there."""
        if path not in self._computed:
            raise UnknownNameError(f"Unknown computed property {path!r}")
        self._publish(events.PropertyChanged(path, None, None, self.get(path)))

    def exposed_elements(self) -> dict[str, Gst.Element]:
        """The elements the model exposes by key: the named ones, or all with `expose_all`.
        The pipeline itself counts too: `playbin name=pb` is the pipeline."""
        return {key: e for key, e in self.element_keys().items() if self._is_exposed(e)}

    def _is_exposed(self, element: Gst.Element) -> bool:
        return self._expose_all or is_user_named(element)

    def _resolve(
        self, path: str, access: Literal["read", "write"]
    ) -> tuple[Gst.Element, Any, Codec]:
        """Like `_locate`, following an alias to its target, which is always exposed. An
        unknown property gets the close ones that can be read or written, as `access` says,
        as its hint."""
        alias = self._declarations().props.get(path)
        if alias:
            return self._locate(alias.target, exposed=False)
        try:
            return self._locate(path)
        except UnknownNameError as exc:
            if str(exc) == f"Unknown property {path!r}":  # not ambiguous or not exposed
                model = self.describe().model_dump(mode="json")
                exc.hint = hints.property_hint(model, path, access)
            raise

    def _locate(self, path: str, *, exposed: bool = True) -> tuple[Gst.Element, Any, Codec]:
        """The element, ParamSpec and codec of an `element.property` path."""
        name, _, prop_name = path.rpartition(".")
        keys = self.element_keys()
        element = keys.get(name)
        if element is None and name:
            same = sorted(key for key, e in keys.items() if e.get_name() == name)
            if len(same) > 1:
                raise UnknownNameError(f"{name!r} is ambiguous: use {' or '.join(same)}")
        ps = element.find_property(prop_name) if element else None
        codec = codec_for(ps) if ps else None
        if element is None or ps is None or codec is None:
            raise UnknownNameError(f"Unknown property {path!r}")
        if exposed and not self._is_exposed(element):
            raise UnknownNameError(f"{name!r} is not exposed: name it or use expose_all")
        return element, ps, codec

    def _read_builtin(self, path: str) -> Any:
        if path == "state":
            return str(self.state.value_nick)
        query = self.pipeline.query_position if path == "position" else self.pipeline.query_duration
        ok, value = query(Gst.Format.TIME)
        return value / Gst.SECOND if ok and value >= 0 else None

    def _publish_position(self) -> bool:
        """Publish `Position` unless it is the last one published; whether it did."""
        current = (self._read_builtin("position"), self._read_builtin("duration"))
        if current == self._last_position:
            return False
        self._last_position = current
        self._publish(events.Position(*current))
        return True

    async def _poll_position(self) -> None:
        """Publish the position every `position_interval` seconds while a stream wants it.
        A failing poll ends, logged the first time; the next position stream starts one."""
        while True:
            await asyncio.sleep(self.position_interval)
            if not self._events.wants_positions:
                self._position_task = None
                return
            try:
                self._publish_position()
            except Exception:
                self._position_task = None
                if not self._position_failed:
                    self._position_failed = True
                    logger.exception("Polling the position failed")
                return

    def _connect_notify(self) -> None:
        """From `setup` to `teardown`, also for the elements added later (decodebin's children):
        publish `PropertyChanged` for the readable properties of the exposed elements, and
        `GraphChanged` and `ModelChanged` when the pipeline's shape changes."""
        self._disconnect_notify()
        for signal, callback in (  # first, so no element is added unseen meanwhile
            ("deep-element-added", self._added),
            ("deep-element-removed", self._removed),
        ):
            self._bin_ids.append(self.pipeline.connect(signal, self._on_element_signal, callback))
        keys = self.element_keys()
        for key, element in keys.items():
            if element != self.pipeline:
                self._watch_shape(element)
            if self._is_exposed(element):
                self._watch(element, key)
        self._graph_seen = self._graph(keys)
        self._exposed_seen = [key for key, e in keys.items() if self._is_exposed(e)]

    def _disconnect_notify(self) -> None:
        for handler in self._bin_ids:
            self.pipeline.disconnect(handler)
        self._bin_ids.clear()
        for element, handler in self._notify_ids:
            element.disconnect(handler)
        self._notify_ids.clear()
        self._watched.clear()
        for gobject, handlers in self._shape_ids.items():
            for handler in handlers:
                gobject.disconnect(handler)
        self._shape_ids.clear()
        if self._shape_flush is not None:
            self._shape_flush.cancel()
            self._shape_flush = None

    def _added(self, element: Gst.Element) -> None:
        self._watch(element)
        self._watch_shape(element)
        self._shape_changed()

    def _removed(self, element: Gst.Element) -> None:
        self._unwatch(element)
        self._unwatch_shape(element)
        self._shape_changed()

    def _on_element_signal(
        self,
        pipeline: Gst.Pipeline,
        sub_bin: Gst.Bin,
        element: Gst.Element,
        callback: Callable[[Gst.Element], None],
    ) -> None:
        """On whatever thread added or removed the element: (un)watch it on the loop."""
        self._call_soon(callback, element)

    def _watch(self, element: Gst.Element, key: str | None = None) -> None:
        if self._loop is None or not self._is_exposed(element):  # torn down meanwhile
            return
        if any(watched == element for watched, _ in self._notify_ids):
            return  # added between connecting deep-element-added and the first walk
        if key is None:  # an element added later: its key is as of now
            key = next((k for k, e in self.element_keys().items() if e == element), None)
            if key is None:
                return  # removed meanwhile
        self._watched[element] = key
        for ps, _ in exposed_props(element):
            if ps.flags & GObject.ParamFlags.READABLE:
                handler = element.connect(f"notify::{ps.name}", self._on_notify, key)
                self._notify_ids.append((element, handler))

    def _unwatch(self, element: Gst.Element) -> None:
        self._watched.pop(element, None)
        for entry in [entry for entry in self._notify_ids if entry[0] == element]:
            element.disconnect(entry[1])
            self._notify_ids.remove(entry)

    # -- links to sometimes pads: again on every play -- #

    def _connect_relinks(self) -> None:
        """From `setup` to `teardown`: link a sometimes pad that comes unlinked to the peer a
        pad of its element had when it came before. `gst_parse` links `decodebin ! queue` on
        the first pad-added only, so without this a pipeline cannot play again after a stop.

        Only the elements the description made: those at the top and in plain bins. Other
        bins (decodebin, playbin) link their children's pads themselves."""
        for element in _description_elements(self.pipeline):
            if any(
                t.direction == Gst.PadDirection.SRC and t.presence == Gst.PadPresence.SOMETIMES
                for t in element.get_pad_template_list()
            ):
                self._relink_ids.append((element, element.connect("pad-added", self._relink)))

    def _disconnect_relinks(self) -> None:
        for element, handler in self._relink_ids:
            element.disconnect(handler)
        self._relink_ids.clear()

    def _relink(self, element: Gst.Element, pad: Gst.Pad) -> None:
        """On the thread that added the pad, before data flows, and after `gst_parse`'s own
        handler, connected first: learn the peer it linked, or link a pad it no longer does."""
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        peers = self._relinks.setdefault(element, [])
        peer = pad.get_peer()
        if peer is not None:
            if peer not in peers:
                peers.append(peer)
            return
        for peer in peers:  # a peer gone from the pipeline fails the link
            if not peer.is_linked() and pad.link(peer) == Gst.PadLinkReturn.OK:
                return

    # -- the pipeline's shape: GraphChanged and ModelChanged -- #

    def _inside(self, gobject: Gst.Object) -> bool:
        """Whether an element or pad is (still) in the pipeline."""
        parent = gobject.get_parent()
        while parent is not None and parent != self.pipeline:
            parent = parent.get_parent()
        return parent is not None

    def _watch_shape(self, element: Gst.Element) -> None:
        """Follow the pads of an element, on the loop: they come and go on streaming threads."""
        if self._loop is None or element in self._shape_ids or not self._inside(element):
            return  # torn down, watched already, or removed again meanwhile
        self._shape_ids[element] = [  # before the walk, so no pad is added unseen meanwhile
            element.connect("pad-added", self._on_pad_signal, self._watch_pad),
            element.connect("pad-removed", self._on_pad_signal, self._unwatch_pad),
        ]
        for pad in _retrying(element.iterate_pads):
            self._watch_pad(pad)

    def _unwatch_shape(self, element: Gst.Element) -> None:
        for gobject in [element, *(o for o in self._shape_ids if o.get_parent() == element)]:
            for handler in self._shape_ids.pop(gobject, ()):
                gobject.disconnect(handler)

    def _watch_pad(self, pad: Gst.Pad) -> None:
        if self._loop is None or pad in self._shape_ids or not self._inside(pad):
            return
        self._shape_ids[pad] = [
            pad.connect(signal, self._on_shape_signal)
            for signal in ("notify::caps", "linked", "unlinked")
        ]
        self._shape_changed()

    def _unwatch_pad(self, pad: Gst.Pad) -> None:
        for handler in self._shape_ids.pop(pad, ()):
            pad.disconnect(handler)
        self._shape_changed()

    def _on_pad_signal(
        self, element: Gst.Element, pad: Gst.Pad, callback: Callable[[Gst.Pad], None]
    ) -> None:
        """On whatever thread added or removed the pad: (un)watch it on the loop."""
        self._call_soon(callback, pad)

    def _on_shape_signal(self, *args: object) -> None:
        """On whatever thread linked, unlinked or negotiated a pad: nothing more than a wakeup
        of the loop, where the changes of `shape_interval` come together."""
        self._call_soon(self._shape_changed)

    def _shape_changed(self) -> None:
        """Publish the pipeline's shape in `shape_interval`, with the changes until then."""
        loop = self._loop
        if loop is not None and self._shape_flush is None:
            self._shape_flush = loop.call_later(self.shape_interval, self._publish_shape)

    def _publish_shape(self) -> None:
        """Publish `GraphChanged` when the graph differs from the last one published, and
        `ModelChanged`, with the commands built again, when the exposed elements or their
        keys do."""
        self._shape_flush = None
        keys = self.element_keys()
        renamed = {e: key for key, e in keys.items() if e in self._watched}
        for element, key in renamed.items():  # a namesake came: `q` is `a.q` now
            if self._watched[element] != key:
                self._unwatch(element)
                self._watch(element, key)
        graph = self._graph(keys)
        if graph != self._graph_seen:
            self._graph_seen = graph
            self._publish(events.GraphChanged(graph))
        exposed = [key for key, e in keys.items() if self._is_exposed(e)]
        if exposed != self._exposed_seen:
            self._exposed_seen = exposed
            # the action signals of the elements that came, and none of those gone (or renamed)
            self._commands, self._computed = self._build_model()
            self._publish(events.ModelChanged(self.describe()))

    def _on_notify(self, element: Gst.Element, ps: Any, key: str) -> None:
        """On whatever thread changed the property: read it there, publish on the loop."""
        codec = codec_for(ps)
        if codec is None:
            return
        value = codec.read(element.get_property(ps.name))
        self._call_soon(
            self._publish, events.PropertyChanged(f"{key}.{ps.name}", key, ps.name, value)
        )

    def _call_soon(self, callback: Callable[..., object], *args: object) -> None:
        """`call_soon_threadsafe` for signal handlers on any thread, a no-op once detached.
        It reads the loop once: `teardown` may clear it on the loop thread meanwhile.
        A loop that closed meanwhile raises RuntimeError; one the garbage collector is
        half way through clearing (signals of a pipeline freed with it) AttributeError."""
        loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError, AttributeError):
                loop.call_soon_threadsafe(callback, *args)

    def _publish(self, event: events.Event) -> None:
        """Publish `event`, and check the commands after it: what it tells may change them."""
        self._events.publish(event)
        if not isinstance(event, events.Position | events.CommandsChanged):
            self._commands_changed()

    def _commands_changed(self) -> None:
        """Check the commands once the loop is done with what it runs now: any number of calls
        in a turn make one check. A no-op while detached."""
        loop = self._loop
        if loop is not None and self._commands_flush is None:
            self._commands_flush = loop.call_soon(self.update_commands)

    def update_commands(self) -> None:
        """Check which commands can run now and publish
        [`CommandsChanged`][mixtape.events.CommandsChanged] when that differs from the last
        time. The player does it after every event, `invoke` and `set`; call it for a condition
        it cannot see (a timer, another thread: `call_soon_threadsafe(player.update_commands)`).
        On the loop thread only; a no-op while detached."""
        if self._commands_flush is not None:
            self._commands_flush.cancel()
            self._commands_flush = None
        if self._loop is None:
            return
        current = self.unavailable()
        if current != self._commands_seen:
            self._commands_seen = current
            self._publish(events.CommandsChanged(dict(current)))

    # -- events and message handlers -- #

    def events(self, position: bool = False) -> events.EventStream:
        """Subscribe to the events published from now on.

        With `position`, the stream also gets [`Position`][mixtape.events.Position] events: the
        current one at once, then one whenever the position or duration changes, read every
        [`position_interval`][mixtape.Player.position_interval].
        """
        self.setup()
        stream = self._events.subscribe(position)
        if position:
            if self._position_task is None:
                assert self._loop is not None
                self._position_task = self._loop.create_task(self._poll_position())
            if not self._publish_position() and self._last_position is not None:
                stream.put(events.Position(*self._last_position))  # only the newcomer lacks it
        return stream

    def add_handler(
        self, message_type: Gst.MessageType, callback: MessageCallback
    ) -> Callable[[], None]:
        """Call `callback(message)` on the loop for bus messages of `message_type`,
        after the built-in handling. Returns a function that removes it again."""
        token = object()
        self._handlers[token] = (message_type, callback)

        def remove() -> None:
            self._handlers.pop(token, None)

        return remove

    # -- bus message handling -- #

    def _drain(self) -> None:
        """The loop's reader callback: handle every message on the bus."""
        while (message := self.bus.pop()) is not None:
            self._handle(message)

    def _handle(self, message: Gst.Message) -> None:
        kind = message.type
        if kind == Gst.MessageType.ERROR:
            self._machine.on_error(message)
        elif kind == Gst.MessageType.EOS:
            self._machine.on_eos()
        elif kind == Gst.MessageType.STATE_CHANGED:
            self._machine.on_state_changed(message)
        elif kind == Gst.MessageType.WARNING:
            self._on_warning(message)
        elif kind == Gst.MessageType.CLOCK_LOST:
            self._machine.on_clock_lost()
        elif kind == Gst.MessageType.LATENCY:
            self.pipeline.recalculate_latency()
        elif kind == Gst.MessageType.BUFFERING:
            self._machine.on_buffering(message)
        elif kind == Gst.MessageType.QOS:
            logger.debug("QoS from %s", message.src.get_name())
        self._publish(events.Message(message))
        for message_type, callback in tuple(self._handlers.values()):
            if kind & message_type:
                try:
                    callback(message)
                except Exception:
                    logger.exception("Message handler %r failed", callback)

    def _on_warning(self, message: Gst.Message) -> None:
        gerror, debug = message.parse_warning()
        source = message.src.get_name() if message.src else "unknown"
        logger.warning("Warning from element %s: %s (%s)", source, gerror.message, debug)
        self._publish(events.Warning(source, gerror.message, debug))
