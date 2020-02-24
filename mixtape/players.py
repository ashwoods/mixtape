import asyncio
import logging
import warnings
import gi
import attr
import typing
import functools
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GObject, GLib  # noqa
from .bus import Bus, PipelineStates

logger = logging.getLogger(__name__)


class AsyncPlayer:


    def __init__(self, pipeline, *args, **kwargs):
        self.pipeline = pipeline  
        
        # TODO: configuration for set_auto_flush_bus
        # as the application depends on async bus messages
        # we want to handle flushing the bus ourselves,
        # otherwise setting the pipeline to `Gst.State.NULL`
        # flushes the bus including the state change messages
        # self.pipeline.set_auto_flush_bus(False)        

        self.bus = None

    @property
    def state(self):
        return self.pipeline.get_state(0)

    @property
    def is_eos(self):
        if self.bus:
            return self.bus.events.eos.is_set()
    
    @property
    def is_error(self):
        if self.bus:
            return self.bus.events.error.is_set()
    
    async def play(self):
        if self.bus is None:
            self._setup()
        self.pipeline.set_state(Gst.State.PLAYING)
        await self.bus.state.wait_for(PipelineStates(Gst.State.PLAYING))

    async def pause(self):
        self.pipeline.set_state(Gst.State.PAUSED)
        await self.bus.state.wait_for(PipelineStates(Gst.State.PAUSED))
    
    async def stop(self, send_eos=True, teardown=True):
        logger.debug("Stopping pipeline...")
        if send_eos and not self.is_eos:
            logger.debug("Sending eos before stop")
            # only send EOS in PLAYING.STATE?
            assert self.state[1] == Gst.State.PLAYING
            await self.send_eos()
            logger.debug("Sent eos before event")
        
        # we don't await the NULL state as it never returns
        # async, and by default it also clears the bus 
        ret = self.pipeline.set_state(Gst.State.NULL)
        logger.debug("Set state to null result %s", ret)
        if teardown:
           self._teardown()
        logger.debug("Stopped pipeline")
        
    async def play_until_eos(self):
        """Will play and exit automatically after eos or error"""
        await self.play()
        await self.bus.events.eos.wait()
        await self.stop(send_eos=False)

    async def send_eos(self):
        self.pipeline.send_event(Gst.Event.new_eos())
        logger.debug("Sent eos message")
        await self.bus.events.eos.wait()

    def _setup(self):
        assert isinstance(self.pipeline, Gst.Pipeline)
        self.bus = Bus(pipeline=self.pipeline)
        logger.debug("Setup complete")

    def _teardown(self):
        """Cleanup player references to loop and gst resources"""
        self.bus.teardown() 
        self.bus = None
        self.pipeline = None
        logger.debug("Teardown complete")


    # -- Helpers -- #

    @classmethod 
    def from_description(cls, description):
        pipeline = Gst.parse_launch(description)
        assert isinstance(pipeline, gi.overrides.Gst.Pipeline)
        return cls(pipeline=pipeline)

    @classmethod
    def as_playbin(cls, uri):
        """A play almost everything playbin Player"""
        playbin = Gst.ElementFactory.make('playbin', 'playbin')
        playbin.set_property('uri', uri)
        p = cls(pipeline=playbin)
        return p 

    def run(self, autoplay=True):
        if autoplay==True:
            asyncio.run(self.play_until_eos())
        else:
            asyncio.run(self.startup())

    # -- Non asyncio helpers -- #

    def call_play(self):
        return asyncio.run_coroutine_threadsafe(self.play(), loop=self.bus.loop)

    def call_stop(self):
        return asyncio.run_coroutine_threadsafe(self.stop(), loop=self.bus.loop)

    def call_pause(self):
        return asyncio.run_coroutine_threadsafe(self.pause(), loop=self.bus.loop)
   
    async def startup(self):
        self._setup()
        await self.bus.events.teardown.wait()



    
    
   

  