"""One subclassable asyncio player for a GStreamer pipeline."""

import asyncio
import concurrent.futures
import contextlib
import inspect
import logging
import warnings
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, ClassVar, Self

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
    Declarations,
    collect,
    command,
    validation_reasons,
)
from ..spec import GraphSpec, PlayerSpec
from .build import ModelBuilder
from .bus import Bus, MessageCallback
from .state import StateMachine
from .watch import Watcher

logger = logging.getLogger(__name__)


def ensure_init() -> None:
    """Initialise GStreamer, once."""
    if not Gst.is_initialized():
        Gst.init(None)


def _invalid(what: str, exc: ValidationError) -> InvalidValueError:
    """An InvalidValueError with pydantic's errors on one line, for a CLI or an HTTP detail."""
    errors = exc.errors(include_url=False, include_context=False)
    return InvalidValueError(f"Invalid {what}: {validation_reasons(exc)}", errors)


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
        self._machine = StateMachine(self)  # the only owner of the pipeline's state
        self._bus = Bus(self, self._machine)
        self._events = events.Broadcast()
        self._model = ModelBuilder(self, expose_all=expose_all)
        self._watch = Watcher(self, self._model)
        self._attached: list[Feature] = []  # in the order they attached
        for alias in self._declarations().props.values():
            self._model.locate(alias.target, exposed=False)  # a typo fails here
        self._model.rebuild()
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
            self._loop = None  # the loop died without a teardown
            self._bus.release()
            self._events = events.Broadcast()  # and its subscribers with it
            self._watch.forget()
            self._machine.forget()  # their futures died with it
            self._watch.disconnect_relinks()  # connected again below
        if self._loop is loop:
            return
        if self._loop is not None:
            raise PlayerNotConfigured("The player is attached to a different loop")
        self._bus.attach(loop)
        self._loop = loop
        self._watch.connect_notify()
        self._watch.connect_relinks()
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
        self._watch.see_commands()  # the first change after this is news

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
        self._watch.disconnect_notify()
        self._watch.disconnect_relinks()
        self._machine.teardown_null(null=null)
        self._detach_features()
        self._machine.reset()
        loop, fd = self._loop, self._bus.release()
        self._loop = None
        self._watch.stop(loop)
        waiters = self._machine.take_waiters()
        if loop is not None and not loop.is_closed():
            self._bus.detach(loop, fd)
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

    def describe(self) -> PlayerSpec:
        """A snapshot of the player's commands, properties and exposed elements."""
        return self._model.describe()

    def unavailable(self) -> dict[str, str]:
        """The commands that cannot run now, with the reason, as of this call: every command's
        [`when`][mixtape.command] check is evaluated, in the order of
        [`describe()`][mixtape.Player.describe]. Needs no running loop."""
        return self._model.unavailable()

    def graph(self) -> GraphSpec:
        """A snapshot of the pipeline's elements and the links between them."""
        return self._model.graph()

    def get(self, path: str) -> Any:
        """Read a property by path: a built-in, a computed property (`feature.name`), an alias
        or `element.property`, as JSON-friendly."""
        if path in BUILTIN_PROPS:
            return self._model.read_builtin(path)
        if (computed := self._model.computed.get(path)) is not None:
            return computed[0].read(computed[1])
        element, ps, codec = self._model.resolve(path, "read")
        if not ps.flags & GObject.ParamFlags.READABLE:
            raise NotReadableError(f"{path!r} is write-only")
        return codec.read(element.get_property(ps.name))

    async def set(self, path: str, value: Any) -> None:
        """Validate `value` and set the property. Nothing changes when it is rejected."""
        self.setup()
        if path in BUILTIN_PROPS or path in self._model.computed:
            raise NotWritableError(f"{path!r} is read-only")
        element, ps, codec = self._model.resolve(path, "write")
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
        if (bound := self._model.commands.get(name)) is None:
            hint = hints.command_hint(self.describe().model_dump(mode="json"), name)
            raise UnknownNameError(f"Unknown command {name!r}", hint=hint)
        declared = bound.declared
        try:
            validated = declared.args.model_validate(args)
        except ValidationError as exc:
            raise _invalid(f"arguments for {name!r}", exc) from exc
        if (reason := self._model.refusal(name, bound)) is not None:
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
        return self._model.element_keys()

    def notify(self, path: str) -> None:
        """Publish the current value of the computed property `path` (`feature.name`) as a
        `PropertyChanged` with no element. On the loop thread only: the events are not
        thread safe, `call_soon_threadsafe` brings a call from another thread there."""
        if path not in self._model.computed:
            raise UnknownNameError(f"Unknown computed property {path!r}")
        self._publish(events.PropertyChanged(path, None, None, self.get(path)))

    def exposed_elements(self) -> dict[str, Gst.Element]:
        """The elements the model exposes by key: the named ones, or all with `expose_all`.
        The pipeline itself counts too: `playbin name=pb` is the pipeline."""
        return self._model.exposed_elements()

    def _shape_changed(self) -> None:
        """Publish the pipeline's shape in `shape_interval`, with the changes until then."""
        self._watch.shape_changed()

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
        self._watch.commands_changed()

    def update_commands(self) -> None:
        """Check which commands can run now and publish
        [`CommandsChanged`][mixtape.events.CommandsChanged] when that differs from the last
        time. The player does it after every event, `invoke` and `set`; call it for a condition
        it cannot see (a timer, another thread: `call_soon_threadsafe(player.update_commands)`).
        On the loop thread only; a no-op while detached."""
        self._watch.update_commands()

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
            self._watch.send_positions(stream)
        return stream

    def add_handler(
        self, message_type: Gst.MessageType, callback: MessageCallback
    ) -> Callable[[], None]:
        """Call `callback(message)` on the loop for bus messages of `message_type`,
        after the built-in handling. Returns a function that removes it again."""
        return self._bus.add_handler(message_type, callback)

    def _drain(self) -> None:
        """Handle every message on the bus."""
        self._bus.drain()
