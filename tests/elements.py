# type: ignore
"""`pyfaulty`, an element that fails on purpose, in Python: what gst-c-crash's `faulty` does
in C, for the tests that run where that plugin is not built.

Its `mode` picks the failure: `start` fails to start, `start-error` too after posting an ERROR
and then a second one, as basesrc follows its subclass's with its own, `start-once` fails the
first start only, `stop` fails READY to
NULL, and `hang` blocks its streaming thread on its first buffer until `RELEASE` is set.
`HANGING` is set once it blocks. `release_all` lets every one go and sets their pipelines to
NULL."""

import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstBase", "1.0")
from gi.repository import GLib, GObject, Gst, GstBase

from mixtape.player import ensure_init

ensure_init()

RELEASE = threading.Event()
HANGING = threading.Event()
_made: "list[Faulty]" = []  # kept until `release_all` stops them: none is disposed in READY


def _any(direction: Gst.PadDirection, name: str) -> Gst.PadTemplate:
    return Gst.PadTemplate.new(name, direction, Gst.PadPresence.ALWAYS, Gst.Caps.new_any())


class Faulty(GstBase.BaseTransform):
    __gstmetadata__ = ("Faulty", "Generic", "Fails on purpose, for tests", "mixtape")
    __gsttemplates__ = (_any(Gst.PadDirection.SINK, "sink"), _any(Gst.PadDirection.SRC, "src"))

    mode = GObject.Property(
        type=str, default="none", blurb="none, start, start-error, start-once, stop or hang"
    )

    def do_start(self):
        _made.append(self)
        if self.mode == "start-error":
            for message in ("Cannot start.", "Failed to start."):
                error = GLib.Error.new_literal(
                    Gst.CoreError.quark(), message, Gst.CoreError.STATE_CHANGE
                )
                self.post_message(Gst.Message.new_error(self, error, "pyfaulty"))
        elif self.mode == "start-once":
            self.mode = "none"
            return False
        return self.mode not in ("start", "start-error")

    def do_transform_ip(self, buffer):
        if self.mode == "hang" and not RELEASE.is_set():
            HANGING.set()
            RELEASE.wait()
        return Gst.FlowReturn.OK

    def do_change_state(self, transition):
        if self.mode == "stop" and transition == Gst.StateChange.READY_TO_NULL:
            return Gst.StateChangeReturn.FAILURE
        return GstBase.BaseTransform.do_change_state(self, transition)


def release_all() -> None:
    """Unblock every `pyfaulty` and set its pipeline to NULL, and clear the events."""
    RELEASE.set()
    for element in list(_made):
        element.mode = "none"
        if (pipeline := element.get_parent()) is not None:
            pipeline.set_state(Gst.State.NULL)
            pipeline.get_bus().set_flushing(False)
    _made.clear()
    RELEASE.clear()
    HANGING.clear()


GObject.type_register(Faulty)
Gst.Element.register(None, "pyfaulty", Gst.Rank.NONE, Faulty)
