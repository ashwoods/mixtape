# type: ignore
import pytest
from time import sleep
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape.base import BasePlayer
from mixtape.exceptions import PlayerSetStateError

SIMPLE_PIPELINE_DESCRIPTION = """videotestsrc ! queue ! fakesink"""
ERROR_PIPELINE_DESCRIPTION = "filesrc ! queue ! fakesink"


@pytest.fixture
def pipeline(Gst):
    """Make sure test pipeline is correct and test env setup"""
    p = Gst.parse_launch(SIMPLE_PIPELINE_DESCRIPTION)
    assert isinstance(p, gi.overrides.Gst.Pipeline)
    return p


def test_base_player_init_and_default_props(pipeline, Gst):
    player = BasePlayer(pipeline=pipeline)

    assert player.pipeline == pipeline
    assert player.state == Gst.State.NULL
    assert isinstance(player.bus, Gst.Bus)


def test_base_player_state_setter_and_getter(Gst, pipeline, mocker):
    player = BasePlayer(pipeline=pipeline)
    spy_setter = mocker.spy(player.pipeline, "set_state")
    player.set_state(Gst.State.READY)
    spy_setter.assert_called_once_with(Gst.State.READY)
    assert spy_setter.spy_return == Gst.StateChangeReturn.SUCCESS
    assert player.state == Gst.State.READY

    player.set_state(Gst.State.PLAYING)
    ret = player.pipeline.get_state(0)
    assert ret == (Gst.StateChangeReturn.ASYNC, Gst.State.READY, Gst.State.PLAYING) or (Gst.StateChangeReturn.SUCCESS, Gst.State.READY, Gst.State.PLAYING)

    sleep(1)  # Player getting started in the background
    assert player.state == Gst.State.PLAYING
    player.set_state(Gst.State.NULL)
    assert player.state == Gst.State.NULL


def test_error_on_state_change(Gst):
    pipeline = Gst.parse_launch(ERROR_PIPELINE_DESCRIPTION)
    player = BasePlayer(pipeline=pipeline)
    with pytest.raises(PlayerSetStateError):
        player.set_state(Gst.State.PLAYING)


@pytest.mark.parametrize(
    "method, state",
    [
        ("ready", Gst.State.READY),
        ("play", Gst.State.PLAYING),
        ("pause", Gst.State.PAUSED),
        ("stop", Gst.State.NULL),
    ],
)
def test_pipeline_state_shortcuts(pipeline, mocker, method, state):
    player = BasePlayer(pipeline=pipeline)
    player.pipeline.set_state = mocker.MagicMock()
    getattr(player, method)()
    player.pipeline.set_state.assert_called_with(state)


def test_player_send_eos(Gst, pipeline, mocker):
    player = BasePlayer(pipeline=pipeline)
    player.pipeline.send_event = mocker.MagicMock()
    player.send_eos()
    player.pipeline.send_event.assert_called()
