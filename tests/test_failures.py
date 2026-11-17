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
from gi.repository import Gst

from mixtape import Player
from mixtape.cli import main
from mixtape.events import StateChanged
from mixtape.exceptions import PlayerPipelineError, PlayerSetStateError
from mixtape.interfaces.http import serve
from tests.elements import HANGING, RELEASE, release_all

HANGS = "videotestsrc ! pyfaulty name=f mode=hang ! fakesink"
ROOT = Path(__file__).parent.parent


class Quick(Player):
    state_timeout = 0.5


@pytest.fixture(autouse=True)
def released():
    yield
    release_all()


def bounded(awaitable):
    return asyncio.wait_for(awaitable, 5)


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
    with pytest.raises(PlayerSetStateError, match="Interrupted"):
        await bounded(play)

    RELEASE.set()  # the change finishes after all, and the player is usable again

    async def stopped() -> None:
        while player.state != Gst.State.NULL:
            await asyncio.sleep(0.01)

    await bounded(stopped())
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
            assert response.status_code == 409 and "stuck changing state" in response.text
            assert time.monotonic() - started < 0.5
            RELEASE.set()


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
