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
    p = Gst.parse_launch(SIMPLE_PIPELINE_DESCRIPTION)
    assert isinstance(p, Gst.Pipeline)
    return p


@pytest.mark.asyncio
async def test_player_init_and_default_props(Gst, pipeline):
    player = Player(pipeline)
    player.setup()
    await player.play()
    await asyncio.sleep(10)
    await player.stop()
