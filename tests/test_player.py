# type: ignore
import asyncio
import pytest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player
from mixtape.exceptions import PlayerSetStateError, PlayerNotConfigured


def test_base_player_init_and_default_props(pipeline):
    player = Player(pipeline=pipeline)
    assert player.pipeline == pipeline
    assert player.state == Gst.State.NULL
    assert len(player.sinks) == 1
    assert len(player.sources) == 1
    assert len(player.elements) == 3
    assert isinstance(player.bus, Gst.Bus)

    t_queue = Gst.ElementFactory.make("queue").get_factory().get_element_type()
    assert len(player.get_elements_by_gtype(t_queue)) == 1


@pytest.mark.asyncio
async def test_error_state_change_before_setup(pipeline):
    player = Player(pipeline=pipeline)
    with pytest.raises(PlayerNotConfigured):
        await player.set_state(Gst.State.PLAYING)


@pytest.mark.asyncio
async def test_gst_error_on_start_exception(Gst):
    pipeline = Gst.parse_launch("filesrc ! fakesink")
    player = Player(pipeline)
    player.setup()

    with pytest.raises(PlayerSetStateError):
        await player.play()
    player.teardown()


@pytest.mark.asyncio
async def test_async_player_sequence(pipeline, mocker):
    player = Player(pipeline)
    player.setup()
    spy = mocker.spy(player.pipeline, "set_state")

    await player.ready()
    spy.assert_called_with(Gst.State.READY)

    await player.play()
    await asyncio.sleep(1)
    spy.assert_called_with(Gst.State.PLAYING)

    await player.pause()
    spy.assert_called_with(Gst.State.PAUSED)

    await player.stop()
    spy.assert_called_with(Gst.State.NULL)


@pytest.mark.asyncio
async def test_player_send_eos(pipeline, mocker):
    player = Player(pipeline=pipeline)
    player.setup()
    spy = mocker.spy(player.pipeline, "send_event")
    await player.play()
    await player.send_eos()
    spy.assert_called()
    await player.stop()
