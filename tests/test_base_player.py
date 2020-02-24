import pytest
import threading
from time import sleep
import os

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GObject, GLib  # noqa


N_BUFFER_PIPELINE_DESCRIPTION = """videotestsrc num-buffers=100 ! tee name=tee ! queue ! autovideosink"""
SIMPLE_PIPELINE_DESCRIPTION   = """videotestsrc ! queue ! autovideosink"""


@pytest.mark.asyncio
async def test_async_player(player):
    p = player.from_description(SIMPLE_PIPELINE_DESCRIPTION)

    await p.play()
    sleep(5)
    assert p.state[1] == Gst.State.PLAYING
    await p.stop()
    assert p.pipeline is None 

def test_player_run(player):
    p = player.from_description(N_BUFFER_PIPELINE_DESCRIPTION)
    p.run()
    assert p.pipeline is None

def test_player_in_thread(player):
    p = player.from_description(SIMPLE_PIPELINE_DESCRIPTION)
    t = threading.Thread(target=lambda: p.run(autoplay=False))
    t.daemon = True
    t.start()
    seq = ['call_play', 'call_pause', 'call_play', 'call_stop']
    for step in seq:
        sleep(2)
        getattr(p, step)()
    t.join()
    assert p.pipeline is None

@pytest.mark.asyncio
async def test_multiple_players(player):
    p = player.from_description(SIMPLE_PIPELINE_DESCRIPTION)
    await p.play()
    sleep(5)
    assert p.state[1] == Gst.State.PLAYING
    await p.stop()
    assert p.pipeline is None
    
    p = player.from_description(SIMPLE_PIPELINE_DESCRIPTION)
    await p.play()
    sleep(5)
    assert p.state[1] == Gst.State.PLAYING
    await p.stop()
    assert p.pipeline is None