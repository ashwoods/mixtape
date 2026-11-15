"""One subclassable asyncio player for a GStreamer pipeline."""

import asyncio
import concurrent.futures
import contextlib
import inspect
import logging
import warnings
from collections.abc import Callable, Coroutine
from typing import Any, ClassVar, Self

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst
from pydantic import NonNegativeFloat, ValidationError

from . import events
from .exceptions import (
    CommandError,
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
from .model import (
    BUILTIN_PROPS,
    Codec,
    Declarations,
    PlayerSpec,
    codec_for,
    collect,
    command,
    describe_element,
    element_prop_spec,
    exposed_props,
    is_named,
)

logger = logging.getLogger(__name__)

MessageCallback = Callable[[Gst.Message], None]


def ensure_init() -> None:
    """Initialise GStreamer, once."""
    if not Gst.is_initialized():
        Gst.init(None)


def _invalid(what: str, exc: ValidationError) -> InvalidValueError:
    """An InvalidValueError with pydantic's errors on one line, for a CLI or an HTTP detail."""
    errors = exc.errors(include_url=False, include_context=False)
    reasons = "; ".join(
        f"{'.'.join(map(str, e['loc']))}: {e['msg']}" if e["loc"] else str(e["msg"]) for e in errors
    )
    return InvalidValueError(f"Invalid {what}: {reasons}", errors)


class Player:
    """Drive a `Gst.Pipeline` from an asyncio loop.

    The player reads the pipeline's bus on the loop it is attached to,
    which happens lazily on the first `await` of any of its methods.
    Subclass it and override the state methods, or react to bus messages
    with `add_handler` and `events()`.

    Its controls and state are described by `describe()`: the properties of the
    named elements (all of them with `expose_all`), the aliases declared with
    `prop()`, the commands declared with `@command`, and the built-ins.
    """

    description: ClassVar[str | None] = None
    """The pipeline description of a subclass that carries its own, for `mixtape run`"""

    _declared: ClassVar[Declarations]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._declarations()  # a bad declaration fails when the class is defined

    @classmethod
    def _declarations(cls) -> Declarations:
        if "_declared" not in cls.__dict__:
            cls._declared = collect(cls, Player)
        return cls._declared

    def __init__(self, pipeline: Gst.Pipeline, *, expose_all: bool = False) -> None:
        ensure_init()
        if not isinstance(pipeline, Gst.Pipeline):
            raise TypeError(f"pipeline must be a Gst.Pipeline, not {type(pipeline).__name__}")
        self.pipeline = pipeline
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fd: int | None = None
        self._error: PlayerPipelineError | None = None
        self._target = Gst.State.NULL  # what the caller asked for, to decide bus policy
        self._live = False
        self._buffering = False
        self._eos = False  # the pipeline reached EOS and has not gone down since
        self._state_waiters: list[tuple[Gst.State, asyncio.Future[None]]] = []
        self._eos_waiters: list[asyncio.Future[None]] = []
        self._handlers: dict[object, tuple[Gst.MessageType, MessageCallback]] = {}
        self._events = events.Broadcast()
        self._expose_all = expose_all
        self._notify_ids: list[tuple[Gst.Element, int]] = []
        for alias in self._declarations().props.values():
            self._locate(alias.target, exposed=False)  # a typo fails here

    @classmethod
    def from_description(cls, description: str, *, expose_all: bool = False) -> Self:
        """Build a player from a `gst-launch` style pipeline description."""
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
        return cls(element, expose_all=expose_all)

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
        """The last pipeline error"""
        return self._error

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
            self._loop = self._fd = None  # the loop died without a teardown
            self._events = events.Broadcast()  # and its subscribers with it
        if self._loop is loop:
            return
        if self._loop is not None:
            raise PlayerNotConfigured("The player is attached to a different loop")
        fd = self.bus.get_pollfd().fd
        loop.add_reader(fd, self._drain)
        self._loop, self._fd = loop, fd
        self._connect_notify()

    def teardown(self) -> None:
        """Set the pipeline to NULL and detach from the loop. Idempotent, re-usable after."""
        self._disconnect_notify()
        self.pipeline.set_state(Gst.State.NULL)
        self._target = Gst.State.NULL
        self._live = self._buffering = self._eos = False
        loop, fd = self._loop, self._fd
        self._loop = self._fd = None
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

    async def __aexit__(self, *exc: object) -> None:
        self.teardown()

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
        self._interrupt_waits(state)
        self._target = state
        self._buffering = False  # the caller's state wins over buffering's pause
        if self.state == state and self.pending == Gst.State.VOID_PENDING:
            return Gst.StateChangeReturn.SUCCESS
        error = self._error
        future = asyncio.get_running_loop().create_future()
        waiter = (state, future)
        self._state_waiters.append(waiter)
        try:
            ret = self.pipeline.set_state(state)
            if state in (Gst.State.NULL, Gst.State.READY):
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
                raise PlayerSetStateError(f"Could not set the pipeline to {state.value_nick}")
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
        """Set the pipeline to NULL, after an EOS when `eos` and it is running."""
        self.setup()
        if eos and self.state in (Gst.State.PAUSED, Gst.State.PLAYING):
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
        """Send an EOS event and wait for the EOS message it causes."""
        await self.send_eos(timeout)

    @command
    def seek(self, position: NonNegativeFloat) -> None:
        """Seek to a position in seconds, flushing."""
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
        if not self.pipeline.seek_simple(Gst.Format.TIME, flags, int(position * Gst.SECOND)):
            raise CommandError(f"The pipeline could not seek to {position} s")

    async def send_eos(self, timeout: float | None = None) -> None:
        """Send an EOS event and wait for the EOS message it causes."""
        self.setup()
        future = asyncio.get_running_loop().create_future()
        self._eos_waiters.append(future)

        async def send_and_wait() -> None:
            # A source blocked on a full downstream holds its lock, and send_event waits for
            # it: send from a thread so the loop (and a second Ctrl-C) stays responsive.
            if not await asyncio.to_thread(self.pipeline.send_event, Gst.Event.new_eos()):
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

    def describe(self) -> PlayerSpec:
        """A snapshot of the player's commands, properties and exposed elements."""
        properties = dict(BUILTIN_PROPS)
        for name, alias in self._declarations().props.items():
            element, ps, codec = self._locate(alias.target, exposed=False)
            properties[name] = alias.spec(element_prop_spec(element, ps, codec))
        return PlayerSpec(
            name=type(self).__name__,
            commands={name: c.spec for name, c in self._declarations().commands.items()},
            properties=properties,
            elements={name: describe_element(e) for name, e in self._exposed_elements().items()},
        )

    def get(self, path: str) -> Any:
        """Read a property by path: a built-in, an alias or `element.property`, as JSON-friendly."""
        if path in BUILTIN_PROPS:
            return self._read_builtin(path)
        element, ps, codec = self._resolve(path)
        if not ps.flags & GObject.ParamFlags.READABLE:
            raise NotReadableError(f"{path!r} is write-only")
        return codec.read(element.get_property(ps.name))

    async def set(self, path: str, value: Any) -> None:
        """Validate `value` and set the property. Nothing changes when it is rejected."""
        self.setup()
        if path in BUILTIN_PROPS:
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
        if (declared := self._declarations().commands.get(name)) is None:
            raise UnknownNameError(f"Unknown command {name!r}")
        try:
            validated = declared.args.model_validate(args)
        except ValidationError as exc:
            raise _invalid(f"arguments for {name!r}", exc) from exc
        result = getattr(self, name)(
            **{k: getattr(validated, k) for k in type(validated).model_fields}
        )
        if inspect.isawaitable(result):
            result = await result
        return None if declared.spec.builtin else result

    def _exposed_elements(self) -> dict[str, Gst.Element]:
        """The elements the model exposes: the named ones, or all with `expose_all`.
        The pipeline itself counts too: `playbin name=pb` is the pipeline."""
        return {
            e.get_name(): e
            for e in (self.pipeline, *self.pipeline.iterate_recurse())
            if self._is_exposed(e)
        }

    def _is_exposed(self, element: Gst.Element) -> bool:
        return self._expose_all or is_named(element)

    def _resolve(self, path: str) -> tuple[Gst.Element, Any, Codec]:
        """Like `_locate`, following an alias to its target, which is always exposed."""
        alias = self._declarations().props.get(path)
        return self._locate(alias.target, exposed=False) if alias else self._locate(path)

    def _locate(self, path: str, *, exposed: bool = True) -> tuple[Gst.Element, Any, Codec]:
        """The element, ParamSpec and codec of an `element.property` path."""
        name, _, prop_name = path.rpartition(".")
        if name and name == self.pipeline.get_name():
            element = self.pipeline  # get_by_name only searches the children
        else:
            element = self.pipeline.get_by_name(name) if name else None
        ps = element.find_property(prop_name) if element else None
        codec = codec_for(ps) if ps else None
        if element is None or ps is None or codec is None:
            raise UnknownNameError(f"Unknown property {path!r}")
        if exposed and name not in self._exposed_elements():
            raise UnknownNameError(f"{name!r} is not exposed: name it or use expose_all")
        return element, ps, codec

    def _read_builtin(self, path: str) -> Any:
        if path == "state":
            return str(self.state.value_nick)
        query = self.pipeline.query_position if path == "position" else self.pipeline.query_duration
        ok, value = query(Gst.Format.TIME)
        return value / Gst.SECOND if ok and value >= 0 else None

    def _connect_notify(self) -> None:
        """Publish `PropertyChanged` for the readable properties of the exposed elements,
        also of those added later (decodebin's children), from `setup` to `teardown`."""
        self._disconnect_notify()
        for signal, callback in (  # first, so no element is added unseen meanwhile
            ("deep-element-added", self._watch),
            ("deep-element-removed", self._unwatch),
        ):
            handler = self.pipeline.connect(signal, self._on_element_signal, callback)
            self._notify_ids.append((self.pipeline, handler))
        for element in self._exposed_elements().values():
            self._watch(element)

    def _disconnect_notify(self) -> None:
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

    def _watch(self, element: Gst.Element) -> None:
        if self._loop is None or not self._is_exposed(element):  # torn down meanwhile
            return
        if any(watched == element for watched, _ in self._notify_ids):
            return  # added between connecting deep-element-added and the first walk
        for ps, _ in exposed_props(element):
            if ps.flags & GObject.ParamFlags.READABLE:
                handler = element.connect(f"notify::{ps.name}", self._on_notify)
                self._notify_ids.append((element, handler))

    def _unwatch(self, element: Gst.Element) -> None:
        for entry in [entry for entry in self._notify_ids if entry[0] == element]:
            element.disconnect(entry[1])
            self._notify_ids.remove(entry)

    def _on_notify(self, element: Gst.Element, ps: Any) -> None:
        """On whatever thread changed the property: read it there, publish on the loop."""
        codec = codec_for(ps)
        if codec is None:
            return
        name = element.get_name()
        value = codec.read(element.get_property(ps.name))
        self._call_soon(
            self._publish, events.PropertyChanged(f"{name}.{ps.name}", name, ps.name, value)
        )

    def _call_soon(self, callback: Callable[..., object], *args: object) -> None:
        """`call_soon_threadsafe` for signal handlers on any thread, a no-op once detached.
        It reads the loop once: `teardown` may clear it on the loop thread meanwhile."""
        loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
                loop.call_soon_threadsafe(callback, *args)

    def _publish(self, event: events.Event) -> None:
        self._events.publish(event)

    # -- events and message handlers -- #

    def events(self) -> events.EventStream:
        """Subscribe to the events published from now on."""
        self.setup()
        return self._events.subscribe()

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
        self.pipeline.set_state(Gst.State.NULL)
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
