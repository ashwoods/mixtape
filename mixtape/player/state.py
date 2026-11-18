"""The player's state machine: the only code that changes the pipeline's state, and the only
owner of what the player knows about it (the changes on their way, the one stuck, the error,
the waits for a state or an EOS)."""

import asyncio
import concurrent.futures
import contextlib
import ctypes
import logging
from collections.abc import Callable
from typing import Protocol

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .. import events
from ..exceptions import MixTapeError, PlayerPipelineError, PlayerSetStateError
from .util import _in_thread, _names, _retrying

logger = logging.getLogger("mixtape.player")  # the player's: the CLI filters on its name

type _Changed = tuple[Gst.State, Gst.StateChangeReturn]  # the state left, what set_state returned

_kept: set[Gst.Pipeline] = set()
"""The pipelines `teardown` left outside NULL, kept on purpose for the life of the process:
GStreamer cannot free an element that has not stopped, and says so with a CRITICAL. Python
may free module globals at exit, before or after the pipeline's other holders, so the set holds
one more reference, which nobody drops."""
ctypes.pythonapi.Py_IncRef(ctypes.py_object(_kept))


class Host(Protocol):
    """What the state machine needs of its player, read at each use: the loop comes and goes
    with `setup` and `teardown`, and a subclass or a test may change `state_timeout`."""

    @property
    def pipeline(self) -> Gst.Pipeline: ...

    @property
    def bus(self) -> Gst.Bus: ...

    @property
    def state(self) -> Gst.State: ...

    @property
    def pending(self) -> Gst.State: ...

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None: ...

    @property
    def state_timeout(self) -> float: ...

    def _publish(self, event: events.Event) -> None: ...

    def _commands_changed(self) -> None: ...

    def _shape_changed(self) -> None: ...

    def _drain(self) -> None: ...


class StateMachine:
    """The state of one player's pipeline: every change to it, the bus messages that change
    it, and the waits for a state or an EOS."""

    def __init__(self, host: Host) -> None:
        self._host = host
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
        self._reports: list[Callable[[], None]] = []  # the errors that wait for their NULL

    @property
    def error(self) -> PlayerPipelineError | None:
        """The first error since the last change to another state than NULL, which stopped
        the pipeline, until the next such change"""
        return self._error

    @property
    def at_eos(self) -> bool:
        """Whether the pipeline reached EOS and has not gone down since"""
        return self._eos

    def flushed(self) -> None:
        """A flushing seek cleared the EOS: the stream goes on."""
        self._eos = False

    # -- setup and teardown -- #

    def forget(self) -> None:
        """Forget the waits and reports of a loop that closed: their futures died with it."""
        self._state_waiters.clear()
        self._eos_waiters.clear()
        self._reports.clear()

    def teardown_null(self, *, null: bool) -> None:
        """Set the pipeline to NULL for a teardown when `null`, unless it is stuck; one that
        fails to stop, or does not within `state_timeout`, is left with a warning, and kept."""
        if null:
            try:
                self.raise_if_stuck()
            except PlayerSetStateError as exc:
                logger.warning("%s: leaving it", exc)
                null = False
        if null:
            call = self._change(Gst.State.NULL)
            try:
                _, ret = call.result(self._host.state_timeout)
            except TimeoutError:
                logger.warning("%s: leaving it", self._mark_stuck(Gst.State.NULL, call))
            else:
                if ret == Gst.StateChangeReturn.FAILURE:
                    logger.warning("%s", self._failed(Gst.State.NULL))
        if self._host.state != Gst.State.NULL:
            _kept.add(self._host.pipeline)

    def reset(self) -> None:
        """Back to what a detached player knows: nothing asked, not live, buffering or at EOS."""
        self._target = Gst.State.NULL
        self._live = self._buffering = self._eos = False

    def take_waiters(self) -> list[asyncio.Future[None]]:
        """The waits for a state or an EOS, which the machine no longer holds."""
        waiters = [future for _, future in self._state_waiters] + self._eos_waiters
        self._state_waiters.clear()
        self._eos_waiters.clear()
        return waiters

    def end_waits(self, waiters: list[asyncio.Future[None]]) -> None:
        """On a teardown with the loop still running: tell the errors on their way, then fail
        `waiters`, the ones `take_waiters` took."""
        for report in list(self._reports):  # an error is told before the end, stopped or not
            report()
        torn_down = PlayerSetStateError("The player was torn down")
        for future in waiters:
            if not future.done():
                future.set_exception(torn_down)
                future.exception()  # mark it retrieved: nobody may be awaiting it

    def clear_reports(self) -> None:
        self._reports.clear()

    # -- state -- #

    async def set_state(self, state: Gst.State) -> Gst.StateChangeReturn:
        """Set the pipeline state, returning once the change is complete."""
        self.raise_if_stuck()  # a refused change changes nothing
        self._interrupt_waits(state)
        self._target = state
        self._buffering = False  # the caller's state wins over buffering's pause
        queued = self._changing is not None and not self._changing.done()
        if (
            not queued
            and self._host.state == state
            and self._host.pending == Gst.State.VOID_PENDING
        ):
            return Gst.StateChangeReturn.SUCCESS
        if state != Gst.State.NULL:
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
            done, _ = await asyncio.wait(
                [change], timeout=self._host.state_timeout if bounded else None
            )
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
                failed: MixTapeError = self._failed(state)  # before a NULL resets them
                self._host._drain()  # the element's ERROR is more useful than a bare failure
                if self._error is not error and self._error is not None:
                    failed = self._error  # and the error set the pipeline to NULL
                elif downward:
                    self._target = self._host.state
                elif self._changing is call:  # else the change asked after it decides
                    # GstBin leaves a failed start half way up: stop it, as an error does
                    self._target = Gst.State.NULL
                    self._live = self._buffering = self._eos = False
                    self._after(self._change(Gst.State.NULL), Gst.State.NULL)
                if not downward:
                    await self._queued_null()
                raise failed
            return ret
        finally:
            with contextlib.suppress(ValueError):  # teardown may have cleared it
                self._state_waiters.remove(waiter)

    async def send_eos(self, timeout: float | None = None) -> None:
        """Send an EOS event and wait for the EOS message it causes."""
        future = asyncio.get_running_loop().create_future()
        self._eos_waiters.append(future)

        async def send_and_wait() -> None:
            # A source blocked on a full downstream holds its lock, and send_event waits for
            # it: send from a thread so the loop (and a second Ctrl-C) stays responsive.
            sent = asyncio.wrap_future(
                _in_thread(self._host.pipeline.send_event, Gst.Event.new_eos())
            )
            await asyncio.wait([sent, future], return_when=asyncio.FIRST_COMPLETED)
            if future.done():  # a stop or an error interrupts a send that is blocked
                return future.result()
            if not sent.result():
                raise MixTapeError("The pipeline did not handle the EOS event")
            await future

        try:
            await asyncio.wait_for(send_and_wait(), timeout)
        finally:
            self.discard_eos_waiter(future)

    def eos_waiter(self) -> asyncio.Future[None]:
        """A future the next EOS sets, and a change to NULL or an error fails; discard it with
        `discard_eos_waiter` when done."""
        future = asyncio.get_running_loop().create_future()
        self._eos_waiters.append(future)
        return future

    def _change(self, state: Gst.State) -> concurrent.futures.Future[_Changed]:
        """Set the pipeline state on a thread, after the changes asked before: its result is
        the state the change left and what `set_state` returned."""

        def change() -> _Changed:
            return self._host.state, self._host.pipeline.set_state(state)

        self._changing = _in_thread(change, after=self._changing)
        self._changing_to = state
        return self._changing

    def stuck_reason(self) -> str | None:
        """Why no change can be made while one that timed out is still blocked, else None."""
        if self._stuck is not None and not self._stuck.done():
            return f"The pipeline is stuck changing state{self._changing_in()}"
        self._stuck = None
        return None

    def raise_if_stuck(self) -> None:
        """Fail at once while a change that timed out is still blocked."""
        if (reason := self.stuck_reason()) is not None:
            raise PlayerSetStateError(reason)

    def eos_refusal(self) -> str | None:
        """Why `eos` cannot run now, else None: paused, the EOS blocks on the prerolled sinks
        (and so would a stop after it); in NULL it waits for the next play; at EOS no second
        one comes."""
        if (reason := self.stuck_reason()) is not None:
            return reason
        if self._host.state != Gst.State.PLAYING or self._eos:
            return "EOS needs a playing pipeline that is not at its EOS"
        return None

    def seek_refusal(self) -> str | None:
        """Why `seek` cannot run now, else None: the pipeline has to be paused or playing
        (in NULL, a source may say it can seek) and to answer that it can."""
        if (reason := self.stuck_reason()) is not None:
            return reason  # no query to a pipeline whose change is blocked
        if self._host.state not in (Gst.State.PAUSED, Gst.State.PLAYING):
            return "Seeking needs a paused or playing pipeline"
        query = Gst.Query.new_seeking(Gst.Format.TIME)
        if not self._host.pipeline.query(query) or not query.parse_seeking()[1]:
            return "The pipeline cannot seek"
        return None

    def _changing_in(self) -> str:
        """`: name, … still changing state` for the elements a change waits for, if any: a
        thread blocked in GStreamer may hold the pipeline's own lock instead."""
        elements = [e for e in _retrying(self._host.pipeline.iterate_recurse) if e.get_state(0)[2]]
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
        self._host._commands_changed()
        return error

    def _after(self, call: concurrent.futures.Future[_Changed], state: Gst.State) -> None:
        """Once `call`, a change to `state` nobody awaits, finishes: warn when it fails to
        stop the pipeline, or update the player when it took it down."""
        loop = self._host.loop

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
        """A change nobody awaits finished: when it went down, do what `set_state` does
        then; the NULL it reached is news only when no change came after it (a later one's
        bus messages tell its own story, and whether it is live)."""
        if self._host.loop is not loop or state not in (Gst.State.NULL, Gst.State.READY):
            return
        self._eos = False  # going down flushed the stream, whatever came after
        if state == Gst.State.NULL and self._changing is call:
            self._live = False
            self._published_null(old)

    def _stuck_error(self, state: Gst.State) -> PlayerSetStateError:
        return PlayerSetStateError(
            f"Could not set the pipeline to {state.value_nick} within "
            f"{self._host.state_timeout:g} s{self._changing_in()}"
        )

    def _failed(self, state: Gst.State) -> PlayerSetStateError:
        """The error of a failed change, naming the elements that failed it when they still
        say so. An element that fails a change up without posting an ERROR may not be named:
        GstBin undoes the change on every child, which resets what the failed one returns,
        and it posts no message (only a tracer hook, which Python cannot get, sees it)."""
        if state == Gst.State.NULL and self._host.state != Gst.State.NULL:
            # GstPipeline flushes it for NULL, even when it fails
            self._host.bus.set_flushing(False)
        failure = Gst.StateChangeReturn.FAILURE
        elements = _retrying(self._host.pipeline.iterate_recurse)
        failed = [e for e in elements if e.get_state(0)[0] == failure]
        leaves = [e for e in failed if not isinstance(e, Gst.Bin)] or failed
        culprits = f": {_names(leaves)} failed" if leaves else ""
        return PlayerSetStateError(f"Could not set the pipeline to {state.value_nick}{culprits}")

    def _set_null(self) -> concurrent.futures.Future[_Changed] | None:
        """Set the pipeline to NULL after an error, on a thread after the changes on their
        way, so a change the error ended does not run after it: a streaming thread that hangs
        may block it. The change that stops it, or None while the player is stuck."""
        if self._stuck is not None and not self._stuck.done():
            return None
        changing = self._changing
        if changing is not None and not changing.done() and self._changing_to == Gst.State.NULL:
            return changing  # a stop on its way takes care of itself
        call = self._change(Gst.State.NULL)
        self._after(call, Gst.State.NULL)
        return call

    async def _queued_null(self) -> None:
        """Wait up to `state_timeout` for the NULL a failed start or its error queued, if it
        is the last change: the start fails once the pipeline is stopped, or stuck."""
        changing = self._changing
        if changing is not None and self._changing_to == Gst.State.NULL and not changing.done():
            done, _ = await asyncio.wait(
                [asyncio.wrap_future(changing)], timeout=self._host.state_timeout
            )
            if not done:
                self._stuck = changing  # as `_mark_stuck`: its `_after` does the rest
                self._host._commands_changed()

    def _published_null(self, old: Gst.State) -> None:
        """Publish the `StateChanged` to NULL the bus cannot: it flushes its messages on
        the way down (unless `auto-flush-bus` is off)."""
        if old != Gst.State.NULL and self._host.pipeline.get_auto_flush_bus():
            self._host._publish(events.StateChanged(old, Gst.State.NULL, Gst.State.VOID_PENDING))
            self._host._shape_changed()

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

    def discard_eos_waiter(self, future: asyncio.Future[None]) -> None:
        if not future.get_loop().is_closed():  # a wait abandoned with its loop ends later
            future.cancel()
        with contextlib.suppress(ValueError):  # teardown may have cleared it
            self._eos_waiters.remove(future)

    # -- bus messages that change the state -- #

    def on_error(self, message: Gst.Message) -> None:
        gerror, debug = message.parse_error()
        source = message.src.get_name() if message.src else "unknown"
        error = PlayerPipelineError(source, gerror.message, debug, gerror)
        if self._error is None:  # the first is the cause: basesrc follows one with its own
            self._error = error
        logger.error("Error from element %s: %s (%s)", source, gerror.message, debug)
        self._target = Gst.State.NULL
        self._live = self._buffering = self._eos = False
        waiters = [future for _, future in self._state_waiters] + self._eos_waiters
        self._state_waiters.clear()  # the error's: an EOS or state still on the bus before the
        self._eos_waiters.clear()  # NULL flushes it does not end them (basesrc posts an EOS)
        call = self._set_null()

        def report() -> None:
            """Fail the waits the error ended, not those asked after it, and publish it."""
            for future in waiters:
                if not future.done():
                    future.set_exception(error)
                    future.exception()  # mark it retrieved: nobody may be awaiting it
            self._host._publish(events.Error(error))

        if call is None:
            report()  # stuck: no NULL to wait for
        else:
            self._report_after(call, report)

    def _report_after(
        self, call: concurrent.futures.Future[_Changed], report: Callable[[], None]
    ) -> None:
        """Run `report` once on the loop when `call`, the NULL an error queued, finishes, or
        after `state_timeout` with the player stuck; and in `teardown`, if it comes first."""
        loop = asyncio.get_running_loop()

        def once() -> None:
            if once in self._reports:
                self._reports.remove(once)
                timer.cancel()
                report()

        def timed_out() -> None:
            if not call.done():
                self._stuck = self._changing  # as `_queued_null`: its `_after` does the rest
                self._host._commands_changed()
            once()

        def finished(_: concurrent.futures.Future[_Changed]) -> None:
            with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
                loop.call_soon_threadsafe(once)

        timer = loop.call_later(self._host.state_timeout, timed_out)
        self._reports.append(once)
        call.add_done_callback(finished)  # after `_after`'s: the NULL is published first

    def on_eos(self) -> None:
        if self._reports:  # the end of a stream an error ended, on the bus before the NULL
            return
        logger.info("EOS")
        self._eos = True
        for future in self._eos_waiters:
            if not future.done():
                future.set_result(None)
        self._host._publish(events.Eos())

    def on_state_changed(self, message: Gst.Message) -> None:
        if message.src != self._host.pipeline:
            return
        old, new, pending = message.parse_state_changed()
        logger.debug("State changed from %s to %s", old.value_nick, new.value_nick)
        if pending == Gst.State.VOID_PENDING:
            _, state, still_pending = self._host.pipeline.get_state(0)  # the message may be stale
            for target, future in self._state_waiters:
                if target == new == state and still_pending == pending and not future.done():
                    future.set_result(None)
        self._host._publish(events.StateChanged(old, new, pending))
        self._host._shape_changed()  # a missed signal shows by the next state at the latest

    def on_clock_lost(self) -> None:
        if self._target == Gst.State.PLAYING:
            logger.info("Clock lost, restarting the clock selection")
            self._change(Gst.State.PAUSED)  # on a thread, as every change
            self._change(Gst.State.PLAYING)

    def on_buffering(self, message: Gst.Message) -> None:
        if self._live:
            return
        percent = message.parse_buffering()
        if percent < 100 and not self._buffering and self._target == Gst.State.PLAYING:
            self._buffering = True
            self._change(Gst.State.PAUSED)
        elif percent == 100 and self._buffering:
            self._buffering = False
            if self._target == Gst.State.PLAYING:
                self._change(Gst.State.PLAYING)
