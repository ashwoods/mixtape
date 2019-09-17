from abc import ABC, abstractmethod
import asyncio
import attr
import gi
gi.require_version('Gst', '1.0')
from abc import ABC, abstractmethod
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GObject, GLib  # noqa
from time import sleep
import logging

logger = logging.getLogger(__name__)



class PlayerBase(ABC):

    def __init__(self, pipeline, features=None, *args, **kwargs):
        """
        Player abstract class. 

        - Creates a gstreamer python application with `pipeline` and enables 
        `features`. 
        - Provides the basic controls of a gstreamer application:
        `play`, `pause`, `stop`. 
        - Provides basic `eos` and error handling.

        Initializes a bus but registering readers or event callbacks 
        is left to the subclasses.

        Parameters
        ----------
        pipeline: `gi.overrides.Gst.Pipeline`
            The gstreamer pipeline object.
        features: `list`
            A list containing feature classes.
        """
        self.pipeline = pipeline
        self._bus = self.pipeline.get_bus()
        self._eos = None
        self._features = []
        if features is None:
            features = [] 
        self._init_features(features)

    @abstractmethod
    def run(self, *args, **kwargs):
        pass

    @abstractmethod
    def cleanup(self):
        pass

    @classmethod 
    def from_description(cls, description: str, features: list=None):
        pipeline = Gst.parse_launch(description)
        assert isinstance(pipeline, gi.overrides.Gst.Pipeline)
        return cls(pipeline=pipeline, features=features)

    def play(self, raise_exception: bool=False) -> bool:
        """
        Set gstreamer pipeline to playing.

        :param raise_exception: Raise a runtime exception in failure cases.
        :return: `True` if the state change was successful.
        """
        return self._set_state(Gst.State.PLAYING, raise_exception)

    def pause(self, raise_exception: bool=False) -> bool:
        logger.debug("Pausing pipeline.")
        return self._set_state(Gst.State.PAUSED, raise_exception)

    def stop(self, raise_exception: bool=False) -> bool:
        logger.debug("Stopping pipeline.")
        self.pipeline.send_event(Gst.Event.new_eos())


    def _set_state(self, state: Gst.State, raise_exception: bool=False) -> bool:
        """
        Set gstreamer pipeline state.

        :param raise_exception: Raise a runtime exception in failure cases.
        :return: `True` if the state change was successful.
        """
        rs = self.pipeline.set_state(state)
        if rs == Gst.StateChangeReturn.SUCCESS:
            return True

        new_state = None
        while rs == Gst.StateChangeReturn.ASYNC:
            rs, new_state, _ = self.pipeline.get_state(state)
        if new_state == state:
            return True

        if raise_exception:
            raise RuntimeError("Unable to set the pipeline to the playing state.")
        return False
    
    def _on_state_changed(self, bus, message):
        old, new, _ = message.parse_state_changed()
        if message.src != self.pipeline:
            return
        logger.debug(
            "State changed from %s to %s", 
            Gst.Element.state_get_name(old),
            Gst.Element.state_get_name(new)
            )

    def _on_error(self, bus, message):
        err, debug = message.parse_error()
        self._exc = err.message 

        logger.error(
            "Error received from element %s:%s", 
            message.src.get_name(), 
            err.message
            )
        if debug is not None:
            logger.error("Debugging information: %s", debug)
        self.cleanup()

    def _on_eos(self, bus, message):
        logger.info("End-Of-Stream reached")
        self.cleanup()

    # -- feature management -- #

    def _init_features(self, features):
        for feature in features:
            f = feature(self.pipeline)
            self._features.append(f)
            setattr(self, f.NAME, f)


class AsyncPlayer(PlayerBase):
    

    def run(self, autoplay=True):
        self.pollfd = self._bus.get_pollfd()
        loop = asyncio.new_event_loop()
        loop.add_reader(self.pollfd.fd, self._poll_cb)
        if autoplay:
            self.play() 
        loop.run_forever()        

    def cleanup(self):
        loop = asyncio.get_event_loop()
        loop.remove_reader(self.pollfd.fd)
        loop.call_soon_threadsafe(loop.stop)
        self._set_state(Gst.State.NULL)

    def _poll_cb(self):
        msg = self._bus.pop()
        if msg:
            if msg.type == Gst.MessageType.ERROR:
                self._on_error(self._bus, msg)
            elif msg.type == Gst.MessageType.EOS:
                self._on_eos(self._bus, msg)
            elif msg.type == Gst.MessageType.STATE_CHANGED:
                self._on_state_changed(self._bus, msg)
            else:
                print(msg.type)
        else:
            self.cleanup()


