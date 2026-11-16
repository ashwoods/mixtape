"""One subclassable asyncio player for a GStreamer pipeline."""

import asyncio
import concurrent.futures
import contextlib
import inspect
import logging
import threading
import warnings
from collections import Counter
from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any, ClassVar, NamedTuple, Self

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst
from pydantic import NonNegativeFloat, ValidationError

from . import events
from .exceptions import (
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
from .features import Feature, FeatureSpec, resolve_all
from .model import (
    BUILTIN_PROPS,
    Codec,
    Command,
    Computed,
    Declarations,
    GraphSpec,
    LinkSpec,
    NodeSpec,
    PlayerSpec,
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

logger = logging.getLogger(__name__)

MessageCallback = Callable[[Gst.Message], None]


def ensure_init() -> None:
    """Initialise GStreamer, once."""
    if not Gst.is_initialized():
        Gst.init(None)


def _retrying[T](iterate: Callable[[], Iterable[T]]) -> list[T]:
    """The items of a GStreamer iterator, walked again when it RESYNCs: the bin or element
    changed under it, from a streaming thread."""
    while True:
        try:
            return list(iterate())
        except Gst.IteratorError:
            continue


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


def _in_thread[T](
    fn: Callable[..., T], *args: object, after: concurrent.futures.Future[Any] | None = None
) -> concurrent.futures.Future[T]:
    """Call `fn(*args)` on a daemon thread, once `after` is done: a call blocked in
    GStreamer (a hung streaming thread holds its locks) blocks neither the loop nor the exit."""
    future: concurrent.futures.Future[T] = concurrent.futures.Future()

    def run() -> None:
        if after is not None:
            concurrent.futures.wait([after])
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(fn(*args))
            except BaseException as exc:
                future.set_exception(exc)

    threading.Thread(target=run, name="mixtape", daemon=True).start()
    return future


type _Changed = tuple[Gst.State, Gst.StateChangeReturn]  # the state left, what set_state returned


def _names(elements: Iterable[Gst.Element]) -> str:
    return ", ".join(element.get_name() for element in elements)


def _invalid(what: str, exc: ValidationError) -> InvalidValueError:
    """An InvalidValueError with pydantic's errors on one line, for a CLI or an HTTP detail."""
    errors = exc.errors(include_url=False, include_context=False)
    return InvalidValueError(f"Invalid {what}: {validation_reasons(exc)}", errors)


class _Bound(NamedTuple):
    """A command of one player: the declaration, whose method `owner` has under `attr`."""

    declared: Command
    owner: object
    attr: str


class Player:
    """Drive a `Gst.Pipeline` from an asyncio loop.

    The player reads the pipeline's bus on the loop it is attached to,
    which happens lazily on the first `await` of any of its methods.
    Subclass it and override the state methods, or react to bus messages
    with `add_handler` and `events()`.

    Its controls and state are described by `describe()`: the properties of the
    named elements (all of them with `expose_all`), the aliases declared with
    `prop()`, the computed properties declared with `@prop`, the commands declared with
    `@command`, and the built-ins. The features it enables add their own, as `feature.name`.
    """

    features: ClassVar[Sequence[str | type[Feature]]] = ()
    """The features every instance enables, as `"name,key=value"` strings or classes: never
    instances, which would be shared. The `features` argument adds to them and wins by name."""
    description: ClassVar[str | None] = None
    """The pipeline description of a subclass that carries its own, for `mixtape run`"""
    position_interval: ClassVar[float] = 0.25
    """Seconds between the reads that publish `Position` to `events(position=True)` streams"""
    state_timeout: ClassVar[float] = 5.0
    """Seconds a change down to READY or NULL may take: a streaming thread that hangs blocks
    it, and the player then calls the pipeline stuck until the change finishes after all"""

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
        if not isinstance(pipeline, Gst.Pipeline):
            raise TypeError(f"pipeline must be a Gst.Pipeline, not {type(pipeline).__name__}")
        self.pipeline = pipeline
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fd: int | None = None
        self._error: PlayerPipelineError | None = None
        self._changing: concurrent.futures.Future[_Changed] | None = None  # the last one
        self._changing_to: Gst.State | None = None  # and its state
        self._stuck: concurrent.futures.Future[_Changed] | None = None
        self._target = Gst.State.NULL  # what the caller asked for, to decide bus policy
        self._live = False
        self._buffering = False
        self._eos = False  # the pipeline reached EOS and has not gone down since
        self._state_waiters: list[tuple[Gst.State, asyncio.Future[None]]] = []
        self._eos_waiters: list[asyncio.Future[None]] = []
        self._handlers: dict[object, tuple[Gst.MessageType, MessageCallback]] = {}
        self._events = events.Broadcast()
        self._position_task: asyncio.Task[None] | None = None  # while a position stream is open
        self._last_position: tuple[float | None, float | None] | None = None  # the last published
        self._expose_all = expose_all
        self._notify_ids: list[tuple[Gst.Element, int]] = []
        self._bin_ids: list[int] = []  # the pipeline's deep-element-added/removed handlers
        self._attached: list[Feature] = []  # in the order they attached
        for alias in self._declarations().props.values():
            self._locate(alias.target, exposed=False)  # a typo fails here
        self._commands, self._computed = self._build_model()
        self._check_features()  # after the model: a feature's `check` may read it

    @classmethod
    def from_description(
        cls, description: str, *, expose_all: bool = False, features: Iterable[FeatureSpec] = ()
    ) -> Self:
        """Build a player from a `gst-launch` style pipeline description.

        The features are prepared first: before GStreamer is initialised, so they can set
        its environment."""
        resolved = list(resolve_all(features, cls.features).values())
        for feature in resolved:
            feature._prepare_once()
        ensure_init()
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
            return cls(element, expose_all=expose_all)
        return cls(element, expose_all=expose_all, features=resolved)

    @classmethod
    def run(cls, description_or_pipeline: str | Gst.Pipeline, timeout: float | None = None) -> None:
        """Play a pipeline until EOS and tear it down, for scripts.

        Raises `PlayerPipelineError` on a pipeline error and `TimeoutError`
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
        """The error that stopped the pipeline last, until it is started again"""
        return self._error

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
        if self._loop is loop:
            return
        if self._loop is not None:
            raise PlayerNotConfigured("The player is attached to a different loop")
        fd = self.bus.get_pollfd().fd
        loop.add_reader(fd, self._drain)
        self._loop, self._fd = loop, fd
        self._connect_notify()
        for feature in self._features.values():
            feature.player = self
            try:
                feature.attach(self)
            except BaseException:
                feature.player = None
                self.teardown()  # detaches those before it
                raise
            self._attached.append(feature)

    def _detach_features(self) -> None:
        """Detach the attached features, the last first. A failing one is logged, not raised."""
        attached, self._attached = self._attached, []
        for feature in reversed(attached):
            try:
                feature.detach(self)
            except Exception:
                logger.exception("Feature %r failed to detach", feature.name)
            feature.player = None

    def teardown(self) -> None:
        """Set the pipeline to NULL and detach from the loop. Idempotent, re-usable after.

        A pipeline that fails to stop, or does not within `state_timeout`, is left as it is
        with a warning. The features detach once it is in NULL, so they see what it left."""
        self._teardown(null=True)

    def _teardown(self, *, null: bool) -> None:
        self._disconnect_notify()
        if null:
            try:
                self._raise_if_stuck()
            except PlayerSetStateError as exc:
                logger.warning("%s: leaving it", exc)
                null = False
        if null:
            call = self._change(Gst.State.NULL)
            try:
                _, ret = call.result(self.state_timeout)
            except TimeoutError:
                logger.warning("%s: leaving it", self._mark_stuck(Gst.State.NULL, call))
            else:
                if ret == Gst.StateChangeReturn.FAILURE:
                    logger.warning("%s", self._failed(Gst.State.NULL))
        self._detach_features()
        self._target = Gst.State.NULL
        self._live = self._buffering = self._eos = False
        loop, fd = self._loop, self._fd
        self._loop = self._fd = None
        if self._position_task is not None and loop is not None and not loop.is_closed():
            self._position_task.cancel()
        self._position_task = self._last_position = None
        waiters = [future for _, future in self._state_waiters] + self._eos_waiters
        self._state_waiters.clear()
        self._eos_waiters.clear()
        if loop is not None and not loop.is_closed():
            if fd is not None:
                loop.remove_reader(fd)
            for future in waiters:
                future.cancel()
            self._events.close()
        else:
            self._events = events.Broadcast()  # its subscribers died with the loop

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
        self._raise_if_stuck()  # a refused change changes nothing
        self._interrupt_waits(state)
        self._target = state
        self._buffering = False  # the caller's state wins over buffering's pause
        queued = self._changing is not None and not self._changing.done()
        if not queued and self.state == state and self.pending == Gst.State.VOID_PENDING:
            return Gst.StateChangeReturn.SUCCESS
        if state in (Gst.State.PAUSED, Gst.State.PLAYING):
            self._error = None  # started again: the error is history
        error = self._error
        future = asyncio.get_running_loop().create_future()
        waiter = (state, future)
        self._state_waiters.append(waiter)
        try:
            call = self._change(state)
            change = asyncio.wrap_future(call)
            downward = state in (Gst.State.NULL, Gst.State.READY)
            bounded = downward or queued  # one behind a change that hangs hangs too
            done, _ = await asyncio.wait([change], timeout=self.state_timeout if bounded else None)
            if not done:
                raise self._mark_stuck(state, call)
            old, ret = change.result()
            if state == Gst.State.NULL and ret != Gst.StateChangeReturn.FAILURE:
                self._published_null(old)
            if downward:
                self._eos = False  # going down flushes the stream
            if state == Gst.State.NULL:
                self._live = False
            elif ret == Gst.StateChangeReturn.NO_PREROLL:
                self._live = True
            if ret == Gst.StateChangeReturn.ASYNC:
                await future
            elif ret == Gst.StateChangeReturn.FAILURE:
                self._target = self.state
                self._drain()  # the element's ERROR is more useful than a bare failure
                if self._error is not error and self._error is not None:
                    raise self._error
                raise self._failed(state)
            return ret
        finally:
            with contextlib.suppress(ValueError):  # teardown may have cleared it
                self._state_waiters.remove(waiter)

    async def ready(self) -> Gst.StateChangeReturn:
        return await self.set_state(Gst.State.READY)

    @command
    async def pause(self) -> Gst.StateChangeReturn:
        """Pause the pipeline."""
        return await self.set_state(Gst.State.PAUSED)

    @command
    async def play(self) -> Gst.StateChangeReturn:
        """Play the pipeline."""
        return await self.set_state(Gst.State.PLAYING)

    @command
    async def stop(self, eos: bool = False, timeout: float | None = 5.0) -> None:
        """Set the pipeline to NULL, after an EOS when `eos` and it is playing.

        A paused pipeline cannot carry the EOS to its sinks (waiting for it blocks), and
        one at its EOS already sends no second one: both stop at once."""
        self.setup()
        if eos and self.state == Gst.State.PLAYING and not self._eos:
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

    @command
    async def eos(self, timeout: float | None = None) -> None:
        """Send an EOS event and wait for the EOS message it causes, while playing."""
        self.setup()
        # paused, the EOS blocks on the prerolled sinks (and so would a stop after it);
        # in NULL it waits for the next play; at EOS no second one comes
        if self.state != Gst.State.PLAYING or self._eos:
            raise CommandError("EOS needs a playing pipeline that is not at its EOS")
        await self.send_eos(timeout)

    @command
    def seek(self, position: NonNegativeFloat) -> None:
        """Seek to a position in seconds, flushing."""
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
        if not self.pipeline.seek_simple(Gst.Format.TIME, flags, int(position * Gst.SECOND)):
            raise CommandError(f"The pipeline could not seek to {position} s")
        self._eos = False  # the flush clears it: the stream goes on

    async def send_eos(self, timeout: float | None = None) -> None:
        """Send an EOS event and wait for the EOS message it causes."""
        self.setup()
        future = asyncio.get_running_loop().create_future()
        self._eos_waiters.append(future)

        async def send_and_wait() -> None:
            # A source blocked on a full downstream holds its lock, and send_event waits for
            # it: send from a thread so the loop (and a second Ctrl-C) stays responsive.
            sent = asyncio.wrap_future(_in_thread(self.pipeline.send_event, Gst.Event.new_eos()))
            await asyncio.wait([sent, future], return_when=asyncio.FIRST_COMPLETED)
            if future.done():  # a stop or an error interrupts a send that is blocked
                return future.result()
            if not sent.result():
                raise MixTapeError("The pipeline did not handle the EOS event")
            await future

        try:
            await asyncio.wait_for(send_and_wait(), timeout)
        finally:
            self._discard_eos_waiter(future)

    async def play_until_eos(self) -> None:
        """Play and return on the next EOS; raise `PlayerPipelineError` on an error.

        A pipeline still at the EOS of an earlier run restarts from READY."""
        self.setup()
        if self._eos:
            await self.ready()
        future = asyncio.get_running_loop().create_future()
        self._eos_waiters.append(future)  # before playing, a short stream may end meanwhile
        try:
            await self.play()
            await future
        finally:
            self._discard_eos_waiter(future)

    def _change(self, state: Gst.State) -> concurrent.futures.Future[_Changed]:
        """Set the pipeline state on a thread, after the changes asked before: its result is
        the state the change left and what `set_state` returned."""

        def change() -> _Changed:
            return self.state, self.pipeline.set_state(state)

        self._changing = _in_thread(change, after=self._changing)
        self._changing_to = state
        return self._changing

    def _raise_if_stuck(self) -> None:
        """Fail at once while a change that timed out is still blocked."""
        if self._stuck is not None and not self._stuck.done():
            raise PlayerSetStateError(f"The pipeline is stuck changing state{self._changing_in()}")
        self._stuck = None

    def _changing_in(self) -> str:
        """`: name, … still changing state` for the elements a change waits for, if any: a
        thread blocked in GStreamer may hold the pipeline's own lock instead."""
        elements = [e for e in _retrying(self.pipeline.iterate_recurse) if e.get_state(0)[2]]
        leaves = [e for e in elements if not isinstance(e, Gst.Bin)] or elements
        return f": {_names(leaves)} still changing" if leaves else ""

    def _mark_stuck(
        self, state: Gst.State, call: concurrent.futures.Future[_Changed]
    ) -> PlayerSetStateError:
        """Call the player stuck until the last change queued finishes, and do what
        `set_state` would have once `call` does: its error is returned."""
        error = self._stuck_error(state)
        self._stuck = self._changing
        self._after(call, state)
        return error

    def _after(self, call: concurrent.futures.Future[_Changed], state: Gst.State) -> None:
        """Once `call`, a change to `state` nobody awaits, finishes: warn when it fails to
        stop the pipeline, or update the player when it took it down."""
        loop = self._loop

        def finished(call: concurrent.futures.Future[_Changed]) -> None:
            old, ret = call.result()
            if ret == Gst.StateChangeReturn.FAILURE:
                if state == Gst.State.NULL:
                    logger.warning("%s", self._failed(state))  # and unflushes the bus
            elif loop is not None:
                with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
                    loop.call_soon_threadsafe(self._went_down, loop, call, state, old)

        call.add_done_callback(finished)

    def _went_down(
        self,
        loop: asyncio.AbstractEventLoop,
        call: concurrent.futures.Future[_Changed],
        state: Gst.State,
        old: Gst.State,
    ) -> None:
        """A change nobody awaits finished: when it went down and no change came after it,
        do what `set_state` does then (a later one's bus messages tell its own story)."""
        if self._loop is not loop or self._changing is not call:
            return
        if state not in (Gst.State.NULL, Gst.State.READY):
            return
        self._eos = False
        if state == Gst.State.NULL:
            self._live = False
            self._published_null(old)

    def _stuck_error(self, state: Gst.State) -> PlayerSetStateError:
        return PlayerSetStateError(
            f"Could not set the pipeline to {state.value_nick} within "
            f"{self.state_timeout:g} s{self._changing_in()}"
        )

    def _failed(self, state: Gst.State) -> PlayerSetStateError:
        """The error of a failed change, naming the elements that failed it when they still
        say so: undoing a failed change up, GstBin may reset what they return."""
        if state == Gst.State.NULL and self.state != Gst.State.NULL:
            self.bus.set_flushing(False)  # GstPipeline flushes it for NULL, even when it fails
        failure = Gst.StateChangeReturn.FAILURE
        elements = _retrying(self.pipeline.iterate_recurse)
        failed = [e for e in elements if e.get_state(0)[0] == failure]
        leaves = [e for e in failed if not isinstance(e, Gst.Bin)] or failed
        culprits = f": {_names(leaves)} failed" if leaves else ""
        return PlayerSetStateError(f"Could not set the pipeline to {state.value_nick}{culprits}")

    def _set_null(self) -> None:
        """Set the pipeline to NULL after an error: on the loop, or after the changes on
        their way, so a change the error ended does not run after it (unless it is stuck)."""
        if self._stuck is not None and not self._stuck.done():
            return
        if self._changing is not None and not self._changing.done():
            if self._changing_to != Gst.State.NULL:  # a stop on its way takes care of itself
                self._after(self._change(Gst.State.NULL), Gst.State.NULL)
            return
        old = self.state
        if self.pipeline.set_state(Gst.State.NULL) == Gst.StateChangeReturn.FAILURE:
            logger.warning("%s", self._failed(Gst.State.NULL))
        else:
            self._published_null(old)

    def _published_null(self, old: Gst.State) -> None:
        """Publish the `StateChanged` to NULL the bus cannot: it flushes its messages on
        the way down (unless `auto-flush-bus` is off)."""
        if old != Gst.State.NULL and self.pipeline.get_auto_flush_bus():
            self._publish(events.StateChanged(old, Gst.State.NULL, Gst.State.VOID_PENDING))

    def _interrupt_waits(self, state: Gst.State) -> None:
        """Fail the waits a change to `state` ends: other targets, and EOS on NULL."""
        futures = [future for target, future in self._state_waiters if target != state]
        if state == Gst.State.NULL:
            futures += self._eos_waiters
        error = PlayerSetStateError(f"Interrupted by a change to {state.value_nick}")
        for future in futures:
            if not future.done():
                future.set_exception(error)
                future.exception()  # mark it retrieved: nobody may be awaiting it

    def _discard_eos_waiter(self, future: asyncio.Future[None]) -> None:
        future.cancel()
        with contextlib.suppress(ValueError):  # teardown may have cleared it
            self._eos_waiters.remove(future)

    # -- the model: properties and commands -- #

    def _check_features(self) -> None:
        """Fail for a feature whose GStreamer plugin features are missing, whose name would
        shadow the `element.property` paths of an element, or that refuses the player."""
        registry = Gst.Registry.get()
        keys = self._element_keys()
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
        """The commands and computed properties of this player: its class's, then each
        feature's as `feature.name`."""
        declared = self._declarations()
        commands = {name: _Bound(c, self, name) for name, c in declared.commands.items()}
        computed: dict[str, tuple[Computed[Any], object]] = {
            name: (c, self) for name, c in declared.computed.items()
        }
        for feature in self._features.values():
            for name, c in feature._declared.commands.items():
                path = f"{feature.name}.{name}"
                renamed = replace(c, spec=c.spec.model_copy(update={"name": path}))
                commands[path] = _Bound(renamed, feature, name)
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
            commands={name: bound.declared.spec for name, bound in self._commands.items()},
            properties=properties,
            elements={key: describe_element(e, key) for key, e in self.exposed_elements().items()},
        )

    def graph(self) -> GraphSpec:
        """A snapshot of the pipeline's elements and the links between them."""
        self.setup()
        keys = {e: key for key, e in self._element_keys().items()}
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
        element, ps, codec = self._resolve(path)
        if not ps.flags & GObject.ParamFlags.READABLE:
            raise NotReadableError(f"{path!r} is write-only")
        return codec.read(element.get_property(ps.name))

    async def set(self, path: str, value: Any) -> None:
        """Validate `value` and set the property. Nothing changes when it is rejected."""
        self.setup()
        if path in BUILTIN_PROPS or path in self._computed:
            raise NotWritableError(f"{path!r} is read-only")
        element, ps, codec = self._resolve(path)
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
        if problems := [w for w in caught if issubclass(w.category, GObject.Warning)]:
            raise InvalidValueError(f"Invalid value for {path!r}: {problems[0].message}")

    async def invoke(self, name: str, /, **args: Any) -> Any:
        """Call the command `name` with validated `args` and return its result.

        Built-in commands return None, as their specs say."""
        self.setup()
        if (bound := self._commands.get(name)) is None:
            raise UnknownNameError(f"Unknown command {name!r}")
        declared = bound.declared
        try:
            validated = declared.args.model_validate(args)
        except ValidationError as exc:
            raise _invalid(f"arguments for {name!r}", exc) from exc
        result = getattr(bound.owner, bound.attr)(
            **{k: getattr(validated, k) for k in type(validated).model_fields}
        )
        if inspect.isawaitable(result):
            result = await result
        return None if declared.spec.builtin else result

    def _element_keys(self) -> dict[str, Gst.Element]:
        """Every element of the pipeline tree by its key: its name when no other element has
        it, else the dotted names from below the pipeline (`a.q`). Over all elements, so the
        keys do not depend on what is exposed. The pipeline's key is always its name, and a
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
        return {key: e for key, e in self._element_keys().items() if self._is_exposed(e)}

    def _is_exposed(self, element: Gst.Element) -> bool:
        return self._expose_all or is_user_named(element)

    def _resolve(self, path: str) -> tuple[Gst.Element, Any, Codec]:
        """Like `_locate`, following an alias to its target, which is always exposed."""
        alias = self._declarations().props.get(path)
        return self._locate(alias.target, exposed=False) if alias else self._locate(path)

    def _locate(self, path: str, *, exposed: bool = True) -> tuple[Gst.Element, Any, Codec]:
        """The element, ParamSpec and codec of an `element.property` path."""
        name, _, prop_name = path.rpartition(".")
        keys = self._element_keys()
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
        """Publish the position every `position_interval` seconds while a stream wants it."""
        while True:
            await asyncio.sleep(self.position_interval)
            if not self._events.wants_positions:
                self._position_task = None
                return
            self._publish_position()

    def _connect_notify(self) -> None:
        """Publish `PropertyChanged` for the readable properties of the exposed elements,
        also of those added later (decodebin's children), from `setup` to `teardown`."""
        self._disconnect_notify()
        for signal, callback in (  # first, so no element is added unseen meanwhile
            ("deep-element-added", self._watch),
            ("deep-element-removed", self._unwatch),
        ):
            self._bin_ids.append(self.pipeline.connect(signal, self._on_element_signal, callback))
        for key, element in self.exposed_elements().items():
            self._watch(element, key)

    def _disconnect_notify(self) -> None:
        for handler in self._bin_ids:
            self.pipeline.disconnect(handler)
        self._bin_ids.clear()
        for element, handler in self._notify_ids:
            element.disconnect(handler)
        self._notify_ids.clear()

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
            key = next((k for k, e in self._element_keys().items() if e == element), None)
            if key is None:
                return  # removed meanwhile
        for ps, _ in exposed_props(element):
            if ps.flags & GObject.ParamFlags.READABLE:
                handler = element.connect(f"notify::{ps.name}", self._on_notify, key)
                self._notify_ids.append((element, handler))

    def _unwatch(self, element: Gst.Element) -> None:
        for entry in [entry for entry in self._notify_ids if entry[0] == element]:
            element.disconnect(entry[1])
            self._notify_ids.remove(entry)

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
        self._events.publish(event)

    # -- events and message handlers -- #

    def events(self, position: bool = False) -> events.EventStream:
        """Subscribe to the events published from now on.

        With `position`, the stream also gets `Position` events: the current one at once,
        then one whenever the position or duration changes, read every `position_interval`.
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
            self._on_error(message)
        elif kind == Gst.MessageType.EOS:
            self._on_eos()
        elif kind == Gst.MessageType.STATE_CHANGED:
            self._on_state_changed(message)
        elif kind == Gst.MessageType.WARNING:
            self._on_warning(message)
        elif kind == Gst.MessageType.CLOCK_LOST:
            self._on_clock_lost()
        elif kind == Gst.MessageType.LATENCY:
            self.pipeline.recalculate_latency()
        elif kind == Gst.MessageType.BUFFERING:
            self._on_buffering(message)
        elif kind == Gst.MessageType.QOS:
            logger.debug("QoS from %s", message.src.get_name())
        self._events.publish(events.Message(message))
        for message_type, callback in tuple(self._handlers.values()):
            if kind & message_type:
                try:
                    callback(message)
                except Exception:
                    logger.exception("Message handler %r failed", callback)

    def _on_error(self, message: Gst.Message) -> None:
        gerror, debug = message.parse_error()
        source = message.src.get_name() if message.src else "unknown"
        error = PlayerPipelineError(source, gerror.message, debug, gerror)
        self._error = error
        logger.error("Error from element %s: %s (%s)", source, gerror.message, debug)
        self._target = Gst.State.NULL
        self._live = self._buffering = self._eos = False
        self._set_null()
        for future in [future for _, future in self._state_waiters] + self._eos_waiters:
            if not future.done():
                future.set_exception(error)
                future.exception()  # mark it retrieved: nobody may be awaiting it
        self._events.publish(events.Error(error))

    def _on_eos(self) -> None:
        logger.info("EOS")
        self._eos = True
        for future in self._eos_waiters:
            if not future.done():
                future.set_result(None)
        self._events.publish(events.Eos())

    def _on_state_changed(self, message: Gst.Message) -> None:
        if message.src != self.pipeline:
            return
        old, new, pending = message.parse_state_changed()
        logger.debug("State changed from %s to %s", old.value_nick, new.value_nick)
        if pending == Gst.State.VOID_PENDING:
            _, state, still_pending = self.pipeline.get_state(0)  # the message may be stale
            for target, future in self._state_waiters:
                if target == new == state and still_pending == pending and not future.done():
                    future.set_result(None)
        self._events.publish(events.StateChanged(old, new, pending))

    def _on_warning(self, message: Gst.Message) -> None:
        gerror, debug = message.parse_warning()
        source = message.src.get_name() if message.src else "unknown"
        logger.warning("Warning from element %s: %s (%s)", source, gerror.message, debug)
        self._events.publish(events.Warning(source, gerror.message, debug))

    def _on_clock_lost(self) -> None:
        if self._target == Gst.State.PLAYING:
            logger.info("Clock lost, restarting the clock selection")
            self.pipeline.set_state(Gst.State.PAUSED)
            self.pipeline.set_state(Gst.State.PLAYING)

    def _on_buffering(self, message: Gst.Message) -> None:
        if self._live:
            return
        percent = message.parse_buffering()
        if percent < 100 and not self._buffering and self._target == Gst.State.PLAYING:
            self._buffering = True
            self.pipeline.set_state(Gst.State.PAUSED)
        elif percent == 100 and self._buffering:
            self._buffering = False
            if self._target == Gst.State.PLAYING:
                self.pipeline.set_state(Gst.State.PLAYING)
