import attr
import asyncio
import gi
import logging
import functools
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GObject, GLib  # noqa
from pampy import match, ANY
logger = logging.getLogger(__name__)


@attr.s(slots=True)
class Bus:
    """
    `Gst.Bus` wrapper for asyncio.

    Takes `Gst.Pipeline` as only argument and requires a `asyncio.loop` bound
    to the current thread as it uses `get_running_loop` during init.

    Polls the gstreamer bus for `Gst.message`s using `asyncio.add_reader` with the 
    file descriptor provided by `Gst.bus.get_pollfd`. 
    
    Used together with the `mixtape.utils.gst_async` on methods that can trigger
    a `Gst.ASYNC` response, it provides a somewhat asyncio compatible interface 
    by wrapping the calls with creation of awaitables that `result` 
    when a `MessageType.ASYNC_DONE` is popped from bus.   

    `Bus.teardown` must be called before setting the pipeline to `Gst.States.NULL`
    and it expects a `Gst.Pipeline` with `set_auto_flush_bus` set to `True`.
    """

    pipeline: Gst.Pipeline = attr.ib()
    bus: Gst.Bus = attr.ib(init=False)
    loop = attr.ib(init=False)
    pollfd = attr.ib(init=False, default=None) 
    futures = attr.ib(init=False, factory=list)
    errors = attr.ib(init=False, factory=list)
    msg = attr.ib(init=False, factory=list)


    @bus.default
    def get_bus(self):
        return self.pipeline.get_bus()

    @loop.default
    def get_loop(self):
        logger.debug("Getting asyncio running loop") 
        return asyncio.get_running_loop()

    def __attrs_post_init__(self):
        self.pollfd = self.bus.get_pollfd()
        self.loop.add_reader(self.pollfd.fd, self.poll_cb) 
    
    def create_async_future(self):
        """
        Creates a future and appends it to `futures`.
        Will `result` with a async_done message.
        """
        ft = self.loop.create_future()
        self.futures.append(ft)
        return ft

    def poll_cb(self):
        """
        Asyncio reader callback. Will get called when a message is available on 
        the bus.
        """
        msg = self.bus.pop()
        if msg:
            handler = match(msg.type,
                Gst.MessageType.ERROR, lambda x: self.on_error,
                Gst.MessageType.EOS, lambda x: self.on_eos,
                Gst.MessageType.STATE_CHANGED, lambda x: self.on_state_changed,
                Gst.MessageType.ASYNC_DONE, lambda x: self.on_async_done, 
                ANY, lambda x: self.on_unhandled_msg)
            handler(self.bus, msg)

    def on_state_changed(self, bus, message):
        """
        Handler for `state_changed` messages
        By default will only log to `debug`
        """
        old, new, _ = message.parse_state_changed()
        if message.src != self.pipeline:
            return
        logger.debug(
            "State changed from %s to %s", 
            Gst.Element.state_get_name(old),
            Gst.Element.state_get_name(new)
            )

    def on_error(self, bus, message):
        """
        Handler for `error` messages
        By default it will parse the error message,
        log to `error` and append to `self.errors`
        """
        err, debug = message.parse_error()
       
        logger.error(
            "Error received from element %s:%s", 
            message.src.get_name(), 
            err.message
            )
        if debug is not None:
            logger.error("Debugging information: %s", debug)
        self.errors.append((err, debug))

    def on_eos(self, bus, message):
        """
        Handler for eos messages
        By default it will only log to `info`
        """
    
        logger.info("End-Of-Stream reached")
        # self._events.eos.set()

    def on_async_done(self, bus, messsage):
        """
        Handler for `async_done` messages
        By default, it will pop any futures available in `self.futures` 
        and call their result.
        """
        
        try:
            ft = self.futures.pop(0)
        except IndexError:
            logger.error("Unexpected ASYNC_DONE message")
        else:
            self._call_from_thread(ft.set_result, None)

    def on_unhandled_msg(self, bus, message):
        """
        Handler for all other messages.
        By default will just log with `debug`
        """
        logger.debug('Unhandled msg: %s', message.type)

    def _call_from_thread(self, callback, *args, **kwargs):
        self.loop.call_soon_threadsafe(functools.partial(callback, *args, **kwargs))

    def teardown(self):
        """Cleanup player references to loop and gst resources"""
        self.loop.remove_reader(self.pollfd.fd)
        self.pollfd = None
        self.bus = None
        self.loop = None
        self.pipeline = None
        logger.debug("Teardown complete")