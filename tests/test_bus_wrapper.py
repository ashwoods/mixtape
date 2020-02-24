import pytest
import threading
from time import sleep
import os
import asyncio
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GObject, GLib  # noqa

from mixtape.bus import Bus


@pytest.mark.asyncio
async def test_bus_wrapper(gst):
    p = Gst.Pipeline()
    src = Gst.ElementFactory.make("videotestsrc", "src")
    q = Gst.ElementFactory.make("queue", "q")
    sink = Gst.ElementFactory.make("autovideosink", "sink")
    p.add(src)
    p.add(q)
    p.add(sink)
    src.link(q)
    q.link(sink)
    bus = Bus(pipeline=p)
    assert bus.loop
    assert bus.pollfd
    res = bus.pipeline.set_state(Gst.State.PLAYING)
    if res == Gst.StateChangeReturn.ASYNC:
        fut = bus.create_async_future()
    await fut
    res = bus.pipeline.set_state(Gst.State.NULL) 
    bus.teardown()
    del p
    