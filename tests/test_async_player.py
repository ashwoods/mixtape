# type: ignore
import asyncio
import pytest

from mixtape.players import AsyncPlayer as Player
from mixtape.exceptions import PlayerSetStateError

SIMPLE_PIPELINE_DESCRIPTION = """videotestsrc ! queue ! fakesink"""
ERROR_PIPELINE_DESCRIPTION = "filesrc ! queue ! fakesink"


@pytest.fixture
def pipeline(Gst):
    """Make sure test pipeline is correct and test env setup"""
    pipeline = Gst.parse_launch(SIMPLE_PIPELINE_DESCRIPTION)
    assert isinstance(pipeline, Gst.Pipeline)
    return pipeline


@pytest.mark.asyncio
async def test_async_player_sequence(Gst, pipeline, mocker):
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

    await player.stop(send_eos=False)
    spy.assert_called_with(Gst.State.NULL)


@pytest.mark.asyncio
async def test_async_player_exception(Gst):
    pipeline = Gst.parse_launch(ERROR_PIPELINE_DESCRIPTION)
    player = Player(pipeline)
    player.setup()

    with pytest.raises(PlayerSetStateError):
        await player.play()
