# type: ignore
import asyncio
import gc
import threading
from collections.abc import Awaitable

import pytest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from mixtape import Player
from mixtape.events import Eos, Error, Message, Position, StateChanged, Warning
from mixtape.exceptions import (
    CommandError,
    MixTapeError,
    PlayerDescriptionError,
    PlayerNotConfigured,
    PlayerPipelineError,
    PlayerSetStateError,
)

LIVE = "videotestsrc is-live=true ! fakesink sync=true"
NEVER_PREROLLS = "appsrc name=src ! fakesink"


def bounded(awaitable: Awaitable):
    """Fail a regression that hangs instead of hanging the suite"""
    return asyncio.wait_for(awaitable, 5)


async def wait_for_event(stream, kind):
    async def find():
        async for event in stream:
            if isinstance(event, kind):
                return event
        raise AssertionError(f"stream ended without a {kind.__name__}")

    return await bounded(find())


def post_error(pipeline: Gst.Pipeline) -> None:
    gerror = GLib.Error.new_literal(Gst.StreamError.quark(), "boom", Gst.StreamError.FAILED)
    src = pipeline.get_by_name("src")
    pipeline.post_message(Gst.Message.new_error(src, gerror, "debug"))


# -- construction -- #


def test_init_and_default_props(pipeline):
    player = Player(pipeline)
    assert player.pipeline == pipeline
    assert player.state == Gst.State.NULL
    assert player.pending == Gst.State.VOID_PENDING
    assert player.loop is None
    assert player.error is None
    assert len(player.sinks) == 1
    assert len(player.sources) == 1
    assert len(player.elements) == 3
    assert isinstance(player.bus, Gst.Bus)

    t_queue = Gst.ElementFactory.make("queue").get_factory().get_element_type()
    assert len(player.get_elements_by_gtype(t_queue)) == 1


def test_from_description_needs_no_loop():
    player = Player.from_description("videotestsrc ! queue ! fakesink")
    assert isinstance(player, Player)
    assert len(player.elements) == 3


def test_init_rejects_other_types():
    with pytest.raises(TypeError):
        Player("videotestsrc ! fakesink")


def test_bad_description():
    with pytest.raises(PlayerDescriptionError) as info:
        Player.from_description("videotestsrc ! nosuchelement")
    assert isinstance(info.value, MixTapeError)
    assert isinstance(info.value.__cause__, GLib.Error)


def test_single_element_description_is_wrapped():
    player = Player.from_description("fakesink")
    assert isinstance(player.pipeline, Gst.Pipeline)
    assert len(player.elements) == 1


# -- attaching -- #


def test_setup_needs_a_running_loop(pipeline):
    with pytest.raises(PlayerNotConfigured):
        Player(pipeline).setup()


@pytest.mark.asyncio
async def test_setup_is_idempotent_and_bound_to_one_loop(pipeline):
    player = Player(pipeline)
    player.setup()
    player.setup()
    assert player.loop is asyncio.get_running_loop()

    async def elsewhere():
        player.setup()

    with pytest.raises(PlayerNotConfigured):
        await bounded(asyncio.to_thread(asyncio.run, elsewhere()))
    player.teardown()
    assert player.loop is None


@pytest.mark.asyncio
async def test_threadsafe_calls_need_attaching(pipeline):
    player = Player(pipeline)
    with pytest.raises(PlayerNotConfigured):
        player.call_soon_threadsafe(print)
    coro = asyncio.sleep(0)
    with pytest.raises(PlayerNotConfigured):
        player.run_coroutine_threadsafe(coro)
    coro.close()


@pytest.mark.asyncio
async def test_run_coroutine_threadsafe_from_a_thread(pipeline):
    async with Player(pipeline) as player:

        async def on_loop():
            return threading.current_thread(), asyncio.get_running_loop()

        def from_thread():
            return player.run_coroutine_threadsafe(on_loop()).result(5)

        thread, loop = await bounded(asyncio.to_thread(from_thread))
        assert thread is threading.main_thread()
        assert loop is player.loop

        called = asyncio.Event()
        await bounded(asyncio.to_thread(player.call_soon_threadsafe, called.set))
        await bounded(called.wait())


# -- state -- #


@pytest.mark.asyncio
async def test_state_sequence(pipeline, mocker):
    async with Player(pipeline) as player:
        spy = mocker.spy(player.pipeline, "set_state")

        await bounded(player.ready())
        spy.assert_called_with(Gst.State.READY)
        assert player.state == Gst.State.READY

        await bounded(player.play())
        spy.assert_called_with(Gst.State.PLAYING)
        assert player.state == Gst.State.PLAYING

        await bounded(player.pause())
        spy.assert_called_with(Gst.State.PAUSED)
        assert player.state == Gst.State.PAUSED

        await bounded(player.stop())
        spy.assert_called_with(Gst.State.NULL)
        assert player.state == Gst.State.NULL


@pytest.mark.asyncio
async def test_set_state_to_the_current_state_is_a_no_op(pipeline, mocker):
    async with Player(pipeline) as player:
        spy = mocker.spy(player.pipeline, "set_state")
        ret = await bounded(player.set_state(Gst.State.NULL))
        assert ret == Gst.StateChangeReturn.SUCCESS
        spy.assert_not_called()


@pytest.mark.asyncio
async def test_play_stop_play_is_not_stale():
    async with Player.from_description(LIVE) as player:
        await bounded(player.play())
        await bounded(player.stop())
        assert player.state == Gst.State.NULL
        await bounded(player.play())
        assert player.state == Gst.State.PLAYING
        assert player.pending == Gst.State.VOID_PENDING


@pytest.mark.asyncio
async def test_stop_on_a_null_player_is_a_no_op(pipeline, mocker):
    async with Player(pipeline) as player:
        spy = mocker.spy(player.pipeline, "set_state")
        await bounded(player.stop())
        await bounded(player.stop(eos=True))
        spy.assert_not_called()
        assert player.state == Gst.State.NULL


@pytest.mark.asyncio
async def test_stop_with_eos_on_a_paused_pipeline_stops_at_once(pipeline, mocker):
    # an EOS cannot reach the prerolled sinks: waiting for it blocked set_state(NULL)
    async with Player(pipeline) as player:
        await bounded(player.play())
        await bounded(player.pause())
        spy = mocker.spy(player, "send_eos")
        await bounded(player.stop(eos=True))
        spy.assert_not_called()
        assert player.state == Gst.State.NULL


@pytest.mark.asyncio
async def test_a_seek_after_the_eos_lets_it_end_again(Gst):
    async with Player.from_description("videotestsrc ! fakesink sync=true") as player:
        await bounded(player.play())
        await bounded(player.invoke("eos", timeout=5))
        with pytest.raises(CommandError):  # one EOS is all a stream gets
            await player.invoke("eos")
        await player.invoke("seek", position=0)

        async def playing_again() -> None:  # the flush loses the state for a moment
            while player.state != Gst.State.PLAYING or player.pending != Gst.State.VOID_PENDING:
                await asyncio.sleep(0.01)

        await bounded(playing_again())
        await bounded(player.invoke("eos", timeout=5))


@pytest.mark.asyncio
async def test_failed_state_change_raises_the_elements_error(error_pipeline):
    async with Player(error_pipeline) as player:
        with pytest.raises(PlayerPipelineError) as info:
            await bounded(player.play())
        assert player.error is info.value
        assert player.state == Gst.State.NULL


# -- errors -- #


@pytest.mark.asyncio
async def test_error_during_a_pending_play_reaches_the_caller():
    unhandled = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: unhandled.append(ctx))
    async with Player.from_description(NEVER_PREROLLS) as player:
        stream = player.events()
        task = asyncio.create_task(player.play())
        await wait_for_event(stream, StateChanged)  # the change is under way
        await bounded(asyncio.to_thread(post_error, player.pipeline))

        with pytest.raises(PlayerPipelineError, match="boom") as info:
            await bounded(task)
        error = info.value
        assert (error.source, error.message, error.debug) == ("src", "boom", "debug")
        assert isinstance(error.gerror, GLib.Error)
        assert str(error) == "src: boom (debug)"
        assert player.error is error
        assert (await wait_for_event(stream, Error)).error is error
        assert player.state == Gst.State.NULL

        # the player stays attached and usable
        assert player.loop is asyncio.get_running_loop()
    gc.collect()
    assert unhandled == []


@pytest.mark.asyncio
async def test_play_until_eos_raises_on_an_error_pipeline(error_pipeline):
    async with Player(error_pipeline) as player:
        with pytest.raises(PlayerPipelineError):
            await bounded(player.play_until_eos())


@pytest.mark.asyncio
async def test_error_ends_a_play_until_eos():
    async with Player.from_description(NEVER_PREROLLS) as player:
        stream = player.events()
        task = asyncio.create_task(player.play_until_eos())
        await wait_for_event(stream, StateChanged)
        await bounded(asyncio.to_thread(post_error, player.pipeline))
        with pytest.raises(PlayerPipelineError, match="boom"):
            await bounded(task)


@pytest.mark.asyncio
async def test_stop_interrupts_a_pending_play_and_play_until_eos():
    async with Player.from_description(NEVER_PREROLLS) as player:
        stream = player.events()
        play = asyncio.create_task(player.play())
        until_eos = asyncio.create_task(player.play_until_eos())
        await wait_for_event(stream, StateChanged)
        await bounded(player.stop())
        for task in (play, until_eos):
            with pytest.raises(PlayerSetStateError, match="Interrupted by a change to null"):
                await bounded(task)
        # the bus flushes its messages on the way down: the player says it reached NULL
        while (event := await wait_for_event(stream, StateChanged)).new != Gst.State.NULL:
            pass
        assert event.pending == Gst.State.VOID_PENDING


# -- eos -- #


@pytest.mark.asyncio
async def test_play_until_eos_again_after_stop():
    async with Player.from_description("videotestsrc num-buffers=10 ! fakesink") as player:
        stream = player.events()
        await bounded(player.play_until_eos())
        await bounded(player.stop())
        await bounded(player.play_until_eos())
        await bounded(player.stop())
    seen = [event async for event in stream]
    assert len([e for e in seen if isinstance(e, Eos)]) == 2


@pytest.mark.asyncio
async def test_play_until_eos_back_to_back():
    async with Player.from_description("videotestsrc num-buffers=10 ! fakesink") as player:
        await bounded(player.play_until_eos())
        await bounded(player.play_until_eos())  # still PLAYING at EOS: restarts from READY


@pytest.mark.asyncio
async def test_pause_while_buffering_stays_paused(pipeline):
    async with Player(pipeline) as player:
        stream = player.events()

        async def buffering(percent):
            player.pipeline.post_message(Gst.Message.new_buffering(None, percent))
            while (await wait_for_event(stream, Message)).message.type != Gst.MessageType.BUFFERING:
                pass

        def heading():
            return player.state if player.pending == Gst.State.VOID_PENDING else player.pending

        await bounded(player.play())
        await buffering(50)
        assert heading() == Gst.State.PAUSED
        while player.state != Gst.State.PAUSED:
            await wait_for_event(stream, StateChanged)
        await bounded(player.pause())  # already PAUSED, by buffering
        await buffering(100)  # the user's pause wins: no resume
        assert heading() == Gst.State.PAUSED


@pytest.mark.asyncio
async def test_send_eos():
    async with Player.from_description(LIVE) as player:
        await bounded(player.play())
        await bounded(player.send_eos())
        await bounded(player.stop())


# -- teardown -- #


def test_there_is_no_destructor():
    assert not hasattr(Player, "__del__")


@pytest.mark.asyncio
async def test_teardown_twice(pipeline):
    player = Player(pipeline)
    await bounded(player.play())
    player.teardown()
    player.teardown()
    assert player.state == Gst.State.NULL
    assert player.loop is None


def test_teardown_after_the_loop_closed(pipeline):
    player = Player(pipeline)
    asyncio.run(bounded(player.play()))
    assert player.state == Gst.State.PLAYING
    player.teardown()
    player.teardown()
    assert player.state == Gst.State.NULL


def test_reuse_after_the_loop_closed(pipeline):
    player = Player(pipeline)
    asyncio.run(bounded(player.play()))  # no teardown: the player is left on a dead loop
    asyncio.run(bounded(player.stop()))
    asyncio.run(bounded(player.play()))
    assert player.state == Gst.State.PLAYING
    player.teardown()


@pytest.mark.asyncio
async def test_teardown_cancels_waiters_and_ends_events():
    player = Player.from_description(NEVER_PREROLLS)
    stream = player.events()
    task = asyncio.create_task(player.play())
    await wait_for_event(stream, StateChanged)
    player.teardown()
    await asyncio.wait({task}, timeout=5)
    assert task.cancelled()
    assert [e async for e in stream]  # ends, after what was already published

    # a later await attaches again
    await bounded(player.ready())
    assert player.loop is asyncio.get_running_loop()
    player.teardown()


# -- events and handlers -- #


@pytest.mark.asyncio
async def test_events(pipeline):
    async with Player(pipeline) as player:
        stream = player.events()
        await bounded(player.play())
        player.pipeline.post_message(
            Gst.Message.new_warning(
                None,
                GLib.Error.new_literal(Gst.StreamError.quark(), "careful", Gst.StreamError.FAILED),
                "dbg",
            )
        )
        seen = []

        async def until_warning():
            async for event in stream:
                seen.append(event)
                if isinstance(event, Warning):
                    return

        await bounded(until_warning())
        await bounded(player.stop())
    changes = [(e.old, e.new) for e in seen if isinstance(e, StateChanged)]
    assert (Gst.State.PAUSED, Gst.State.PLAYING) in changes
    assert any(isinstance(e, Message) for e in seen)
    (warning,) = [e for e in seen if isinstance(e, Warning)]
    assert (warning.message, warning.debug) == ("careful", "dbg")


class FastPositions(Player):
    position_interval = 0.02


@pytest.mark.asyncio
async def test_position_events_only_for_position_streams_polled_lazily():
    player = FastPositions.from_description(LIVE)
    async with player:
        plain = player.events()
        assert player._position_task is None  # nobody asked for positions
        stream = player.events(position=True)
        task = player._position_task
        assert task is not None
        first = await bounded(anext(stream))
        assert first == Position(None, None)  # immediately, though nothing runs
        await bounded(player.play())
        positions = [first]
        while len({p.position for p in positions}) < 3:
            positions.append(await wait_for_event(stream, Position))
        assert all(p.position is None or p.position >= 0 for p in positions)

        # a second subscriber is told where it is at once; the poll keeps its one task
        late = player.events(position=True)
        assert isinstance((await bounded(anext(late))), Position)
        assert player._position_task is task

        stream.close()
        late.close()
        await asyncio.sleep(0.1)  # the next tick finds nobody and ends the task
        assert player._position_task is None and task.done()
        await bounded(player.stop())
    assert not [e async for e in plain if isinstance(e, Position)]


@pytest.mark.asyncio
async def test_a_paused_pipeline_publishes_its_position_once_and_teardown_ends_the_poll():
    player = FastPositions.from_description(LIVE)
    async with player:
        await bounded(player.pause())
        stream = player.events(position=True)
        task = player._position_task
        await bounded(anext(stream))
        await asyncio.sleep(0.2)
        # unchanged pairs are not published again
        assert sum(isinstance(e, Position) for e in stream._queue._queue) <= 1
    await asyncio.sleep(0)
    assert player._position_task is None and task.cancelled()


class Bly(Player):
    """The pattern of an application subclassing the player"""

    def __init__(self, pipeline, extra):
        super().__init__(pipeline)
        self.extra = extra
        self.calls = []
        self.applications = asyncio.Queue()
        self.add_handler(Gst.MessageType.APPLICATION, self.applications.put_nowait)

    async def play(self):
        self.calls.append("play")
        return await super().play()


@pytest.mark.asyncio
async def test_subclass_with_extra_args_override_and_handler(pipeline):
    player = Bly(pipeline, "extra")
    async with player:
        await bounded(player.play())
        assert player.calls == ["play"]
        assert player.extra == "extra"

        structure = Gst.Structure.new_empty("hello")
        player.pipeline.post_message(Gst.Message.new_application(player.pipeline, structure))
        message = await bounded(player.applications.get())
        assert message.get_structure().get_name() == "hello"


@pytest.mark.asyncio
async def test_handlers_survive_each_other_and_can_be_removed(pipeline, caplog):
    seen = []

    def broken(message):
        raise RuntimeError("handler bug")

    async with Player(pipeline) as player:
        removers = [
            player.add_handler(Gst.MessageType.APPLICATION, broken),
            player.add_handler(Gst.MessageType.APPLICATION, lambda m: seen.append("one")),
            player.add_handler(Gst.MessageType.APPLICATION | Gst.MessageType.EOS, seen.append),
        ]
        stream = player.events()

        def post():
            player.pipeline.post_message(
                Gst.Message.new_application(None, Gst.Structure.new_empty("x"))
            )

        post()
        await wait_for_event(stream, Message)
        assert seen[0] == "one"
        assert len(seen) == 2
        assert "handler bug" in caplog.text

        for remove in removers:
            remove()
            remove()  # idempotent
        post()
        await wait_for_event(stream, Message)
        assert len(seen) == 2
