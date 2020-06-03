import logging
from typing import Any, Callable, Iterable, Optional, Tuple, Type, TypeVar

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

    def __del__(self) -> None:
        """
        Make sure that the gstreamer pipeline is always cleaned up
        """
        if self.state is not Gst.State.NULL:
            logger.warning("Player cleanup on destructor")
            self.teardown()

    @property
    def bus(self) -> Gst.Bus:
        return self.pipeline.get_bus()

    @property
    def state(self) -> Gst.State:
        return self.pipeline.get_state(0)[1]

    def set_state(self, state: Gst.State) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        """Set pipeline state"""
        ret = self.pipeline.set_state(state)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise PlayerSetStateError
        return ret

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        if self.state is not Gst.State.NULL:
            self.set_state(Gst.State.NULL)

    def ready(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        return self.set_state(Gst.State.READY)

    def play(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        return self.set_state(Gst.State.PLAYING)

    def pause(self) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        return self.set_state(Gst.State.PAUSED)

    def stop(
        self, send_eos: bool = False, teardown: bool = False
    ) -> Tuple[Gst.StateChangeReturn, Gst.State, Gst.State]:
        if send_eos:
            self.send_eos()

        ret = self.set_state(Gst.State.NULL)

        if teardown:
            self.teardown()

        return ret

    def send_eos(self) -> bool:
        return self.pipeline.send_event(Gst.Event.new_eos())

    @classmethod
    def create(cls: Type[BasePlayerType], pipeline: Gst.Pipeline) -> BasePlayerType:
        player = cls(pipeline)
        player.setup()
        return player

    @classmethod
    def from_description(cls: Type[BasePlayerType], description: str) -> BasePlayerType:
        pipeline = Gst.parse_launch(description)
        assert isinstance(pipeline, Gst.Pipeline)
        return cls.create(pipeline=pipeline)
