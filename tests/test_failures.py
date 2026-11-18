# type: ignore
"""How the player and `mixtape run` handle a pipeline that hangs or fails to change state,
with `pyfaulty` (tests/elements.py)."""

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from mixtape import Player
from mixtape.cli import main
from mixtape.events import Error, Message, StateChanged
from mixtape.exceptions import PlayerPipelineError, PlayerSetStateError
from mixtape.interfaces.http import serve
from tests.elements import HANGING, RELEASE, release_all
from tests.flaky import timing

HANGS = "videotestsrc ! pyfaulty name=f mode=hang ! fakesink"
HANGS_PLAYING = "videotestsrc ! pyfaulty name=f mode=hang ! fakesink async=false"
ROOT = Path(__file__).parent.parent
# a bound of half a second (state_timeout) on the loop's answer: the hang the change waits for is
# released after 2 s, and `not RELEASE.is_set()` checks strictly that it was not waited for
quick = pytest.mark.flaky(reason="a busy machine can hold the loop for half a second")


class Quick(Player):
    state_timeout = 0.5


@pytest.fixture(autouse=True)
def released():
    yield
    release_all()


def bounded(awaitable):
    return asyncio.wait_for(awaitable, 5)


async def next_event(stream, kind, where=lambda event: True):
    async def find():
        async for event in stream:
            if isinstance(event, kind) and where(event):
                return event
        raise AssertionError(f"stream ended without a {kind.__name__}")

    return await bounded(find())


async def tick(longest: list[float]) -> None:
    """Keep in `longest[0]` the longest the loop went without running this task."""
    last = time.monotonic()
    while True:
        await asyncio.sleep(0.01)
        now = time.monotonic()
        longest[0], last = max(longest[0], now - last), now


@pytest.mark.asyncio
async def test_a_stop_that_hangs_names_the_element_and_frees_the_loop():
    player = Quick.from_description(HANGS)
    player.setup()
    play = asyncio.create_task(player.play())
    assert await asyncio.to_thread(HANGING.wait, 5)

    with pytest.raises(PlayerSetStateError, match="to null within 0.5 s: f still changing"):
        await bounded(player.stop())
    with pytest.raises(PlayerSetStateError, match="stuck changing state: f still changing"):
        await player.play()  # at once
    unavailable = player.unavailable()
    for name in ("play", "pause", "stop", "eos"):
        assert unavailable[name].startswith("The pipeline is stuck changing state: f still")
    with pytest.raises(PlayerSetStateError, match="Interrupted"):
        await bounded(play)

    RELEASE.set()  # the change finishes after all, and the player is usable again

    async def stopped() -> None:
        while player.state != Gst.State.NULL:
            await asyncio.sleep(0.01)

    await bounded(stopped())
    assert not {"play", "pause", "stop"} & set(player.unavailable())
    await bounded(player.stop())
    player.teardown()


@pytest.mark.asyncio
async def test_a_failed_stop_names_the_element_and_leaves_the_bus_working():
    async with Player.from_description("videotestsrc ! pyfaulty name=f mode=stop ! fakesink") as p:
        await bounded(p.play())
        with pytest.raises(PlayerSetStateError, match="to null: f failed"):
            await bounded(p.stop())
        assert p.state == Gst.State.READY

        # GstPipeline flushed its bus for NULL: a play waited for messages that never came
        p.pipeline.get_by_name("f").props.mode = "none"
        await bounded(p.play())
        assert p.state == Gst.State.PLAYING


@pytest.mark.asyncio
async def test_a_failed_start_is_raised_and_stops_the_pipeline():
    async with Player.from_description("videotestsrc ! pyfaulty name=f mode=start ! fakesink") as p:
        stream = p.events()
        # not always naming f: GstBin's undo of the failed change may reset what it returns
        with pytest.raises(PlayerSetStateError, match="Could not set the pipeline to playing"):
            await bounded(p.play())
        # GstBin's undo made the pipeline commit PAUSED, half way up: it is stopped instead
        assert (p.state, p.pending) == (Gst.State.NULL, Gst.State.VOID_PENDING)
        assert p.get("state") == "null"  # what HTTP reads
        stream.close()
        changes = [e async for e in stream if isinstance(e, StateChanged)]
        assert changes[-1].new == Gst.State.NULL  # what the page shows


@pytest.mark.asyncio
async def test_a_failed_start_leaves_the_change_asked_after_it_to_decide():
    description = "videotestsrc ! pyfaulty name=f mode=start-once ! fakesink"
    async with Player.from_description(description) as p:
        first, second = asyncio.create_task(p.play()), asyncio.create_task(p.play())
        with pytest.raises(PlayerSetStateError, match="Could not set the pipeline to playing"):
            await bounded(first)
        await bounded(second)  # the failure's NULL used to run after it and hang its wait
        assert p.state == Gst.State.PLAYING


@pytest.mark.asyncio
async def test_a_start_that_posts_errors_raises_the_first():
    description = "videotestsrc ! pyfaulty name=f mode=start-error ! fakesink"
    async with Player.from_description(description) as p:
        for _ in range(3):  # the second error, posted as the change fails, used to win at times
            with pytest.raises(PlayerPipelineError) as raised:
                await bounded(p.play())
            assert (raised.value.source, raised.value.message) == ("f", "Cannot start.")
            assert p.error is raised.value
            assert p.state == Gst.State.NULL


def test_a_failed_stop_keeps_the_pipeline_instead_of_disposing_it_outside_null():
    run = (
        "import sys, tests.elements; from mixtape.cli import main; "
        "code = main(['run', 'videotestsrc num-buffers=5 ! pyfaulty name=f mode=stop "
        "! fakesink']); "
        "tests.elements._made.clear(); sys.exit(code)"  # the player holds the last reference
    )
    done = subprocess.run(
        [sys.executable, "-c", run], capture_output=True, text=True, timeout=20, cwd=ROOT
    )
    assert done.returncode == 1
    assert "error: could not set the pipeline to null: f failed" in done.stderr
    assert "CRITICAL" not in done.stderr, done.stderr


@quick
@pytest.mark.asyncio
async def test_eos_on_a_hung_pipeline_answers_409_and_the_loop_runs_on():
    hangs_playing = "videotestsrc is-live=true ! pyfaulty name=f mode=hang ! fakesink async=false"
    player = Quick.from_description(hangs_playing)
    async with player, serve(player, "127.0.0.1", 0) as listening:
        async with httpx.AsyncClient(base_url=listening.url, timeout=5) as client:
            assert (await client.post("/api/commands/play")).status_code == 200
            assert await asyncio.to_thread(HANGING.wait, 5)
            eos = asyncio.create_task(client.post("/api/commands/eos", json={"timeout": 0.5}))
            properties = await client.get("/api/properties")  # meanwhile
            assert (properties.status_code, eos.done()) == (200, False)
            response = await eos
            assert (response.status_code, response.json()) == (
                409,
                {"detail": "No EOS within 0.5 s"},
            )

            stop = await client.post("/api/commands/stop")
            assert (stop.status_code, stop.json()) == (
                409,
                {"detail": "Could not set the pipeline to null within 0.5 s"},
            )
            started = time.monotonic()
            response = await client.post("/api/commands/eos")  # at once while stuck
            took = time.monotonic() - started
            assert response.status_code == 409 and "stuck changing state" in response.text
            assert not RELEASE.is_set()
            RELEASE.set()
    timing(took < 0.5, f"the answer took {took:.2f} s")


@quick
@pytest.mark.asyncio
async def test_an_error_while_a_streaming_thread_hangs_leaves_the_loop_free():
    player = Quick.from_description(HANGS_PLAYING)
    player.setup()
    stream = player.events()
    until_eos = asyncio.create_task(player.play_until_eos())
    assert await asyncio.to_thread(HANGING.wait, 5)
    longest = [0.0]
    ticker = asyncio.create_task(tick(longest))
    threading.Timer(2, RELEASE.set).start()  # from a thread: the loop used to wait for it

    f = player.pipeline.get_by_name("f")
    gerror = GLib.Error.new_literal(Gst.StreamError.quark(), "boom", Gst.StreamError.FAILED)
    f.post_message(Gst.Message.new_error(f, gerror, "debug"))
    # the NULL the error queues blocks on the hung thread: the error is raised after
    # state_timeout, with the player stuck, and a change asked meanwhile is refused
    with pytest.raises(PlayerPipelineError, match="boom"):
        await bounded(until_eos)
    assert (await next_event(stream, Error)).error is player.error
    assert not RELEASE.is_set()
    with pytest.raises(PlayerSetStateError, match="stuck changing state"):
        await player.play()

    await next_event(stream, StateChanged, lambda event: event.new == Gst.State.NULL)
    ticker.cancel()
    await bounded(player.play())  # usable again once the NULL finished
    await bounded(player.stop())
    player.teardown()
    timing(longest[0] < Quick.state_timeout, f"the loop was held for {longest[0]:.2f} s")


@quick
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["buffering", "clock-lost"])
async def test_buffering_and_a_lost_clock_change_state_off_the_loop(kind):
    player = Quick.from_description(HANGS_PLAYING)
    player.setup()
    stream = player.events()
    await bounded(player.play())
    assert await asyncio.to_thread(HANGING.wait, 5)
    # a stop on another thread holds the pipeline's state lock until the hang ends
    threading.Thread(target=player.pipeline.set_state, args=(Gst.State.NULL,), daemon=True).start()
    await next_event(stream, StateChanged, lambda event: event.new == Gst.State.PAUSED)
    threading.Timer(2, RELEASE.set).start()

    message = (
        Gst.Message.new_buffering(None, 50)
        if kind == "buffering"
        else Gst.Message.new_clock_lost(None, Gst.SystemClock.obtain())
    )
    started = time.monotonic()
    player.pipeline.post_message(message)
    await next_event(stream, Message, lambda event: event.message.type == message.type)
    took = time.monotonic() - started
    assert not RELEASE.is_set()  # handled without the lock

    await bounded(asyncio.to_thread(RELEASE.wait))
    await bounded(player.stop())  # after the changes it queued
    player.teardown()
    timing(took < Quick.state_timeout, f"the {kind} message took {took:.2f} s")


def test_run_exits_naming_the_element_that_hangs(monkeypatch, capsys):
    monkeypatch.setattr(Player, "state_timeout", 0.5)

    def interrupt() -> None:
        if HANGING.wait(5):
            os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()
    assert main(["run", HANGS]) == 1  # it hangs in the preroll: no EOS to wait for
    assert "error: could not set the pipeline to null within 0.5 s: f still changing" in (
        capsys.readouterr().err
    )


def test_run_fails_when_the_pipeline_cannot_stop(capsys):
    assert main(["run", "videotestsrc num-buffers=5 ! pyfaulty name=f mode=stop ! fakesink"]) == 1
    assert "error: could not set the pipeline to null: f failed" in capsys.readouterr().err
