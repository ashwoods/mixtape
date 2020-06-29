import logging
from typing import Tuple, Type, TypeVar

import attr
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .exceptions import PlayerSetStateError


logger = logging.getLogger(__name__)

BasePlayerType = TypeVar("BasePlayerType", bound="BasePlayer")


@attr.s
class BasePlayer:
    """Player base player"""

    # TODO: configuration for set_auto_flush_bus
    # as the application depends on async bus messages
    # we might want to handle flushing the bus ourselves,
    # otherwise setting the pipeline to `Gst.State.NULL`
    # flushes the bus including the state change messages
    # self.pipeline.set_auto_flush_bus(False)

    pipeline: Gst.Pipeline = attr.ib()
    init: bool = attr.ib(init=False, default=False)

    def __del__(self) -> None:
        """
        Make sure that the gstreamer pipeline is always cleaned up
        """
        if self.state is not Gst.State.NULL:
            logger.warning("Player cleanup on destructor")
            self.teardown()

    @property
    def bus(self) -> Gst.Bus:
        """Convenience property for the pipeline Gst.Bus"""
        return self.pipeline.get_bus()

    @property
    def state(self) -> Gst.State:
        """Convenience property for the current pipeline Gst.State"""
        return self.pipeline.get_state(0)[1]

    def set_state(self, state: Gst.State) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Set pipeline state"""
        if not self.init:
            logger.warning("Calling set_state without calling setup. Trying to do this now.")
            self.setup()
        ret = self.pipeline.set_state(state)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise PlayerSetStateError
        return ret

    def setup(self) -> None:
        """
        Player setup: meant to be used with hooks or subclassed
        Call super() after custom code.
        """
        self.init = True

    def teardown(self) -> None:
        """Player teardown: by default sets the pipeline to Gst.State.NULL"""
        if self.state is not Gst.State.NULL:
            self.set_state(Gst.State.NULL)

    def ready(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Set pipeline to state to Gst.State.READY"""
        return self.set_state(Gst.State.READY)

    def play(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Set pipeline to state to Gst.State.PLAY"""
        return self.set_state(Gst.State.PLAYING)

    def pause(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Set pipeline to state to Gst.State.PAUSED"""
        return self.set_state(Gst.State.PAUSED)

    # fmt: off
    def stop(self, send_eos: bool = False, teardown: bool = False) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Set pipeline to state to Gst.State.NULL, with the option of sending eos and teardown"""
    # fmt: on
        if send_eos:
            self.send_eos()

        ret = self.set_state(Gst.State.NULL)

        if teardown:
            self.teardown()

        return ret

    def send_eos(self) -> bool:
        """Send a eos event to the pipeline"""
        return self.pipeline.send_event(Gst.Event.new_eos())

    @classmethod
    def create(cls: Type[BasePlayerType], pipeline: Gst.Pipeline) -> BasePlayerType:
        """Player factory from a given pipeline that calls setup by default"""
        player = cls(pipeline)
        player.setup()
        return player

    @classmethod
    def from_description(cls: Type[BasePlayerType], description: str) -> BasePlayerType:
        """Player factory from a pipeline description"""
        pipeline = Gst.parse_launch(description)
        assert isinstance(pipeline, Gst.Pipeline)
        return cls.create(pipeline=pipeline)
