# type: ignore
"""How the player and `mixtape run` handle a pipeline that hangs or fails to change state,
with `pyfaulty` (tests/elements.py)."""

import asyncio
import os
import signal
import threading

import pytest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player
from mixtape.cli import main
from mixtape.exceptions import PlayerSetStateError
from tests.elements import HANGING, RELEASE, release_all

HANGS = "videotestsrc ! pyfaulty name=f mode=hang ! fakesink"


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
async def test_a_failed_start_is_raised():
    async with Player.from_description("videotestsrc ! pyfaulty name=f mode=start ! fakesink") as p:
        # not always naming f: GstBin's undo of the failed change may reset what it returns
        with pytest.raises(PlayerSetStateError, match="Could not set the pipeline to playing"):
            await bounded(p.play())


def test_run_exits_naming_the_element_that_hangs(monkeypatch, capsys):
    monkeypatch.setattr(Player, "state_timeout", 0.5)

    def interrupt() -> None:
        if HANGING.wait(5):
            os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()
    assert main(["run", HANGS]) == 1  # it hangs in the preroll: no EOS to wait for
    assert "error: Could not set the pipeline to null within 0.5 s: f still changing" in (
        capsys.readouterr().err
    )


def test_run_fails_when_the_pipeline_cannot_stop(capsys):
    assert main(["run", "videotestsrc num-buffers=5 ! pyfaulty name=f mode=stop ! fakesink"]) == 1
    assert "error: Could not set the pipeline to null: f failed" in capsys.readouterr().err
