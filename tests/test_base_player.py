import pytest
import threading
from time import sleep
import os


N_BUFFER_PIPELINE_DESCRIPTION = """videotestsrc num-buffers=100 ! tee name=tee ! queue ! fakevideosink"""
SIMPLE_PIPELINE_DESCRIPTION   = """videotestsrc ! fakevideosink"""


def test_async_player(player):
    p = player.from_description(N_BUFFER_PIPELINE_DESCRIPTION)
    p.run()

def test_background_thread_async_player(player):
    p = player.from_description(SIMPLE_PIPELINE_DESCRIPTION)
    t = threading.Thread(target=lambda: p.run(autoplay=False))
    t.daemon = True
    t.start()
    seq = ['play', 'pause', 'play', 'stop']
    for step in seq:
        getattr(p, step)()
        sleep(2)
    sleep(1)
    t.join()