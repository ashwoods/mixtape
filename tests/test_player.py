# type: ignore
import asyncio
import gc
import threading
from collections.abc import Awaitable

import pytest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from mixtape import Feature, Player, command
from mixtape.events import (
    CommandsChanged,
    Eos,
    Error,
    Message,
    Position,
    StateChanged,
    Warning,
)
from mixtape.exceptions import (
    CommandError,
    MixTapeError,
    PlayerDescriptionError,
    PlayerNotConfigured,
    PlayerPipelineError,
    PlayerSetStateError,
)
from tests.players import Recorder

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


def test_from_description_defaults_to_the_class_description():
    class Card(Player):
        description = "videotestsrc name=src ! fakesink name=sink"

    assert sorted(e.get_name() for e in Card.from_description().elements) == ["sink", "src"]
    with pytest.raises(PlayerDescriptionError, match="Player has no description"):
        Player.from_description()


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
        assert player.state == Gst.State.NULL
        seen = []
        while not isinstance(event := await wait_for_event(stream, StateChanged | Error), Error):
            seen.append(event.new)
        assert event.error is error
        assert seen[-1] == Gst.State.NULL  # the Error comes once the player stopped it

        # the player stays attached and usable
        assert player.loop is asyncio.get_running_loop()
    gc.collect()
    assert unhandled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sink", ["fakesink", "fakesink async=false"], ids=["async", "at-once"])
async def test_a_state_is_published_before_the_wait_for_it_returns(sink: str):
    # what a WebSocket's reply to `play` relies on to come after the states
    async with Player.from_description(f"videotestsrc ! {sink}") as player:
        stream = player.events()
        await player.play()
        published = []
        while not stream._queue.empty():
            published.append(stream._queue.get_nowait())
        assert [e.new for e in published if isinstance(e, StateChanged)][-1] == Gst.State.PLAYING


@pytest.mark.asyncio
async def test_an_eos_after_an_error_does_not_end_the_wait_the_error_ended():
    async with Player.from_description("videotestsrc name=src is-live=true ! fakesink") as player:
        stream = player.events()
        until_eos = asyncio.create_task(player.play_until_eos())
        while player.state != Gst.State.PLAYING:
            await wait_for_event(stream, StateChanged)
        post_error(player.pipeline)  # basesrc follows a flow error with an EOS
        player.pipeline.post_message(Gst.Message.new_eos(player.pipeline))
        with pytest.raises(PlayerPipelineError, match="boom"):
            await bounded(until_eos)
        kinds = []
        while not isinstance(event := await wait_for_event(stream, Eos | Error), Error):
            kinds.append(type(event))
        assert kinds == []  # the stream the error ended has no end of its own
        assert not player._machine._eos


@pytest.mark.asyncio
async def test_a_play_right_after_an_error_wins_over_the_stop_it_queued():
    async with Player.from_description("videotestsrc name=src is-live=true ! fakesink") as player:
        stream = player.events()
        until_eos = asyncio.create_task(player.play_until_eos())
        await wait_for_event(stream, StateChanged)
        post_error(player.pipeline)  # on the loop: handled on its next turn

        async def errored() -> None:
            while player.error is None:
                await asyncio.sleep(0)

        await bounded(errored())
        await bounded(player.play())  # at once, behind the NULL the error queued
        assert (player.state, player.error) == (Gst.State.PLAYING, None)
        with pytest.raises(PlayerPipelineError, match="boom"):
            await bounded(until_eos)  # the wait the error ended, not the play after it
        await wait_for_event(stream, Error)
        assert (player.state, player.error) == (Gst.State.PLAYING, None)


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
@pytest.mark.parametrize("decoder", ["decodebin", "decodebin3"])
async def test_a_description_links_a_decoders_pad_again_on_every_play(decoder: str):
    # gst_parse links the first pad-added only: the next play would stop with not-linked
    description = f"audiotestsrc num-buffers=10 ! wavenc ! {decoder} ! queue ! fakesink"
    player = Player.from_description(description)
    async with player:
        await bounded(player.play_until_eos())
        await bounded(player.stop())
        await bounded(player.play_until_eos())
    async with player:  # attached again
        await bounded(player.play_until_eos())


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
            # one read: the change can finish on a streaming thread between two
            _, state, pending = player.pipeline.get_state(0)
            return state if pending == Gst.State.VOID_PENDING else pending

        await bounded(player.play())
        await buffering(50)
        while player.state != Gst.State.PAUSED:  # changed on the worker thread
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


def test_reuse_after_a_loop_closed_without_cancelling_its_waits():
    description = "audiotestsrc num-buffers=50 ! wavenc ! decodebin ! queue ! fakesink sync=true"
    player = Player.from_description(description)
    loop = asyncio.new_event_loop()
    loop.create_task(player.play_until_eos())
    loop.run_until_complete(asyncio.sleep(0.2))  # playing, its EOS a second away
    loop.close()  # its waits stay: resolving one used to raise "Event loop is closed"
    relinks = len(player._watch._relink_ids)
    asyncio.run(bounded(player.play_until_eos()))
    assert len(player._watch._relink_ids) == relinks == 1  # connected again, not once more
    player.teardown()


@pytest.mark.asyncio
async def test_teardown_fails_waiters_and_ends_events():
    player = Player.from_description(NEVER_PREROLLS)
    stream = player.events()
    task = asyncio.create_task(player.play())
    await wait_for_event(stream, StateChanged)
    player.teardown()
    with pytest.raises(PlayerSetStateError, match="^The player was torn down$"):
        await bounded(task)
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
        assert player._watch._position_task is None  # nobody asked for positions
        stream = player.events(position=True)
        task = player._watch._position_task
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
        assert player._watch._position_task is task

        stream.close()
        late.close()
        await asyncio.sleep(0.1)  # the next tick finds nobody and ends the task
        assert player._watch._position_task is None and task.done()
        await bounded(player.stop())
    assert not [e async for e in plain if isinstance(e, Position)]


@pytest.mark.asyncio
async def test_a_failing_position_poll_is_logged_once_and_starts_again(caplog, monkeypatch):
    player = FastPositions.from_description(LIVE)
    async with player:
        for _ in range(2):
            player.events(position=True)
            task = player._watch._position_task
            with monkeypatch.context() as patched:
                patched.setattr(player._watch, "_publish_position", lambda: 1 / 0)
                await asyncio.wait({task}, timeout=5)
            assert task.done() and player._watch._position_task is None
        assert caplog.text.count("Polling the position failed") == 1

        stream = player.events(position=True)  # a new stream polls again
        await bounded(player.play())
        await wait_for_event(stream, Position)
        assert (await wait_for_event(stream, Position)).position is not None


@pytest.mark.asyncio
async def test_a_paused_pipeline_publishes_its_position_once_and_teardown_ends_the_poll():
    player = FastPositions.from_description(LIVE)
    async with player:
        await bounded(player.pause())
        stream = player.events(position=True)
        task = player._watch._position_task
        await bounded(anext(stream))
        await asyncio.sleep(0.2)
        # unchanged pairs are not published again
        assert sum(isinstance(e, Position) for e in stream._queue._queue) <= 1
    await asyncio.sleep(0)
    assert player._watch._position_task is None and task.cancelled()


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


# -- command availability -- #

NOT_PLAYING = "EOS needs a playing pipeline that is not at its EOS"
NOT_PAUSED = "Seeking needs a paused or playing pipeline"
OGG_PUSH = "videotestsrc num-buffers=100 ! theoraenc ! oggmux ! decodebin ! fakesink"


@pytest.mark.asyncio
async def test_the_builtin_commands_that_cannot_run_now(Gst):
    player = Player.from_description("audiotestsrc ! fakesink")
    assert player.unavailable() == {"eos": NOT_PLAYING, "seek": NOT_PAUSED}  # no loop needed
    assert player.describe().unavailable == player.unavailable()
    async with player:
        stream = player.events()
        await bounded(player.pause())
        assert player.unavailable() == {"eos": NOT_PLAYING}
        await bounded(player.play())
        assert player.unavailable() == {}
        while (await wait_for_event(stream, CommandsChanged)).unavailable:
            pass  # paused, then playing: published as they come

    async with Player.from_description(OGG_PUSH) as player:  # an Ogg stream cannot seek paused
        await bounded(player.pause())
        assert player.unavailable() == {"eos": NOT_PLAYING, "seek": "The pipeline cannot seek"}


@pytest.mark.asyncio
async def test_a_command_that_cannot_run_is_refused_and_its_change_published(Gst):
    async with Recorder.from_description(Recorder.description) as player:
        stream = player.events()
        with pytest.raises(CommandError, match="^Not recording$"):
            await player.invoke("stop_recording")
        assert player.unavailable()["stop_recording"] == "Not recording"

        await player.invoke("start_recording")
        event = await wait_for_event(stream, CommandsChanged)
        assert "stop_recording" not in event.unavailable
        assert event.unavailable == player.unavailable()
        await asyncio.sleep(0.05)  # the same map is not published again
        stream.close()
        assert not [e async for e in stream if isinstance(e, CommandsChanged)]
        await player.invoke("stop_recording")


@pytest.mark.asyncio
async def test_a_feature_command_is_unavailable_as_feature_name_command(Gst):
    class Gated(Feature):
        name = "gated"

        @command(when=lambda self: False)
        def go(self) -> None: ...

    player = Player.from_description("fakesink", features=[Gated()])
    # before setup, as `mixtape inspect` asks: its check would see no player
    assert player.unavailable()["gated.go"] == "The feature is not attached to a running player"
    async with player:
        assert player.unavailable()["gated.go"] == "'gated.go' cannot run now"
        with pytest.raises(CommandError, match="cannot run now"):
            await player.invoke("gated.go")


@pytest.mark.asyncio
async def test_a_check_that_raises_makes_the_command_unavailable_and_is_logged_once(Gst, caplog):
    class Broken(Player):
        @command(when=lambda self: 1 / 0)
        def boom(self) -> None: ...

    player = Broken.from_description("fakesink")
    for _ in range(2):
        assert player.unavailable()["boom"] == "'boom' could not be checked: division by zero"
    assert caplog.text.count("The check of the command 'boom' failed") == 1


@pytest.mark.parametrize("verdict", [None, "", 0])
def test_a_check_that_returns_neither_bool_nor_a_reason_is_an_error(Gst, caplog, verdict):
    class Vague(Player):
        @command(when=lambda self: verdict)
        def go(self) -> None: ...

    player = Vague.from_description("fakesink")
    expected = f"'go' could not be checked: it returned {verdict!r}, not True, False or a reason"
    for _ in range(2):
        assert player.unavailable()["go"] == expected
    assert caplog.text.count("The check of the command 'go' failed") == 1
