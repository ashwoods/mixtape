"""The player's live model: the property changes, the pipeline's shape and the commands it
publishes as they change, the links to sometimes pads it makes again, and the position."""

import asyncio
import logging
from collections.abc import Callable, Iterator
from typing import Any, Protocol

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GObject, Gst

from .. import events
from ..model import codec_for, exposed_props
from ..spec import GraphSpec, PlayerSpec
from .build import ModelBuilder
from .util import _retrying

logger = logging.getLogger("mixtape.player")  # the player's: the CLI filters on its name


def _description_elements(bin: Gst.Bin) -> Iterator[Gst.Element]:
    """The elements of `bin` and, deep, of its plain bins: those a description can make."""
    plain = (Gst.Bin.__gtype__, Gst.Pipeline.__gtype__)
    for element in _retrying(bin.iterate_elements):
        yield element
        if isinstance(element, Gst.Bin) and element.__gtype__ in plain:
            yield from _description_elements(element)


class Host(Protocol):
    """What the live model needs of its player, read at each use: the loop and the events
    come and go with `setup` and `teardown`, and a subclass or a test may change the
    intervals. Its public methods are the player's, so a subclass that overrides one is what
    the live model calls."""

    @property
    def pipeline(self) -> Gst.Pipeline: ...

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None: ...

    @property
    def shape_interval(self) -> float: ...

    @property
    def position_interval(self) -> float: ...

    @property
    def _events(self) -> events.Broadcast: ...

    def _publish(self, event: events.Event) -> None: ...

    def _call_soon(self, callback: Callable[..., object], *args: object) -> None: ...

    def element_keys(self) -> dict[str, Gst.Element]: ...

    def describe(self) -> PlayerSpec: ...

    def unavailable(self) -> dict[str, str]: ...

    def update_commands(self) -> None: ...


class Watcher:
    """What one player publishes as the pipeline changes, from `setup` to `teardown`."""

    def __init__(self, host: Host, model: ModelBuilder) -> None:
        self._host = host
        self._model = model  # what it builds again when the exposed elements change
        self._position_task: asyncio.Task[None] | None = None  # while a position stream is open
        self._last_position: tuple[float | None, float | None] | None = None  # the last published
        self._notify_ids: list[tuple[Gst.Element, int]] = []
        self._watched: dict[Gst.Element, str] = {}  # the key its notify handlers publish
        self._shape_ids: dict[Gst.Object, list[int]] = {}  # element and pad handlers
        self._shape_flush: asyncio.TimerHandle | None = None  # a publication on its way
        self._commands_flush: asyncio.Handle | None = None  # a check of the commands on its way
        self._commands_seen: dict[str, str] = {}  # `unavailable()` as last published or seen
        self._position_failed = False  # a failing poll of the position was logged
        self._graph_seen: GraphSpec | None = None  # as last published
        self._exposed_seen: list[str] = []  # the keys of the exposed elements, likewise
        self._bin_ids: list[int] = []  # the pipeline's deep-element-added/removed handlers
        self._relink_ids: list[tuple[Gst.Element, int]] = []  # pad-added, to link again
        self._relinks: dict[Gst.Element, list[Gst.Pad]] = {}  # the peers its new pads had

    # -- setup and teardown -- #

    def forget(self) -> None:
        """Forget the position poll and the check of the commands of a loop that closed: they
        died with it."""
        self._position_task = self._last_position = None
        self._commands_flush = None

    def see_commands(self) -> None:
        """Take the commands as they are now as seen: the first change after this is news."""
        self._commands_seen = self._host.unavailable()

    def stop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """On a teardown from `loop`: stop the position poll, unless it died with the loop,
        and the check of the commands on its way."""
        if self._position_task is not None and loop is not None and not loop.is_closed():
            self._position_task.cancel()
        self._position_task = self._last_position = None
        if self._commands_flush is not None:
            self._commands_flush.cancel()
            self._commands_flush = None

    # -- the position -- #

    def send_positions(self, stream: events.EventStream) -> None:
        """Poll the position for `stream`, a new one that wants it, and give it the current
        one at once."""
        if self._position_task is None:
            assert self._host.loop is not None
            self._position_task = self._host.loop.create_task(self._poll_position())
        if not self._publish_position() and self._last_position is not None:
            stream.put(events.Position(*self._last_position))  # only the newcomer lacks it

    def _publish_position(self) -> bool:
        """Publish `Position` unless it is the last one published; whether it did."""
        current = (self._model.read_builtin("position"), self._model.read_builtin("duration"))
        if current == self._last_position:
            return False
        self._last_position = current
        self._host._publish(events.Position(*current))
        return True

    async def _poll_position(self) -> None:
        """Publish the position every `position_interval` seconds while a stream wants it.
        A failing poll ends, logged the first time; the next position stream starts one."""
        while True:
            await asyncio.sleep(self._host.position_interval)
            if not self._host._events.wants_positions:
                self._position_task = None
                return
            try:
                self._publish_position()
            except Exception:
                self._position_task = None
                if not self._position_failed:
                    self._position_failed = True
                    logger.exception("Polling the position failed")
                return

    # -- property changes -- #

    def connect_notify(self) -> None:
        """From `setup` to `teardown`, also for the elements added later (decodebin's children):
        publish `PropertyChanged` for the readable properties of the exposed elements, and
        `GraphChanged` and `ModelChanged` when the pipeline's shape changes."""
        self.disconnect_notify()
        for signal, callback in (  # first, so no element is added unseen meanwhile
            ("deep-element-added", self._added),
            ("deep-element-removed", self._removed),
        ):
            self._bin_ids.append(
                self._host.pipeline.connect(signal, self._on_element_signal, callback)
            )
        keys = self._host.element_keys()
        for key, element in keys.items():
            if element != self._host.pipeline:
                self._watch_shape(element)
            if self._model.is_exposed(element):
                self._watch(element, key)
        self._graph_seen = self._model.graph_of(keys)
        self._exposed_seen = [key for key, e in keys.items() if self._model.is_exposed(e)]

    def disconnect_notify(self) -> None:
        for handler in self._bin_ids:
            self._host.pipeline.disconnect(handler)
        self._bin_ids.clear()
        for element, handler in self._notify_ids:
            element.disconnect(handler)
        self._notify_ids.clear()
        self._watched.clear()
        for gobject, handlers in self._shape_ids.items():
            for handler in handlers:
                gobject.disconnect(handler)
        self._shape_ids.clear()
        if self._shape_flush is not None:
            self._shape_flush.cancel()
            self._shape_flush = None

    def _added(self, element: Gst.Element) -> None:
        self._watch(element)
        self._watch_shape(element)
        self.shape_changed()

    def _removed(self, element: Gst.Element) -> None:
        self._unwatch(element)
        self._unwatch_shape(element)
        self.shape_changed()

    def _on_element_signal(
        self,
        pipeline: Gst.Pipeline,
        sub_bin: Gst.Bin,
        element: Gst.Element,
        callback: Callable[[Gst.Element], None],
    ) -> None:
        """On whatever thread added or removed the element: (un)watch it on the loop."""
        self._host._call_soon(callback, element)

    def _watch(self, element: Gst.Element, key: str | None = None) -> None:
        if self._host.loop is None or not self._model.is_exposed(element):  # torn down meanwhile
            return
        if any(watched == element for watched, _ in self._notify_ids):
            return  # added between connecting deep-element-added and the first walk
        if key is None:  # an element added later: its key is as of now
            key = next((k for k, e in self._host.element_keys().items() if e == element), None)
            if key is None:
                return  # removed meanwhile
        self._watched[element] = key
        for ps, _ in exposed_props(element):
            if ps.flags & GObject.ParamFlags.READABLE:
                handler = element.connect(f"notify::{ps.name}", self._on_notify, key)
                self._notify_ids.append((element, handler))

    def _unwatch(self, element: Gst.Element) -> None:
        self._watched.pop(element, None)
        for entry in [entry for entry in self._notify_ids if entry[0] == element]:
            element.disconnect(entry[1])
            self._notify_ids.remove(entry)

    def _on_notify(self, element: Gst.Element, ps: Any, key: str) -> None:
        """On whatever thread changed the property: read it there, publish on the loop."""
        codec = codec_for(ps)
        if codec is None:
            return
        value = codec.read(element.get_property(ps.name))
        self._host._call_soon(
            self._host._publish, events.PropertyChanged(f"{key}.{ps.name}", key, ps.name, value)
        )

    # -- links to sometimes pads: again on every play -- #

    def connect_relinks(self) -> None:
        """From `setup` to `teardown`: link a sometimes pad that comes unlinked to the peer a
        pad of its element had when it came before. `gst_parse` links `decodebin ! queue` on
        the first pad-added only, so without this a pipeline cannot play again after a stop.

        Only the elements the description made: those at the top and in plain bins. Other
        bins (decodebin, playbin) link their children's pads themselves."""
        for element in _description_elements(self._host.pipeline):
            if any(
                t.direction == Gst.PadDirection.SRC and t.presence == Gst.PadPresence.SOMETIMES
                for t in element.get_pad_template_list()
            ):
                self._relink_ids.append((element, element.connect("pad-added", self._relink)))

    def disconnect_relinks(self) -> None:
        for element, handler in self._relink_ids:
            element.disconnect(handler)
        self._relink_ids.clear()

    def _relink(self, element: Gst.Element, pad: Gst.Pad) -> None:
        """On the thread that added the pad, before data flows, and after `gst_parse`'s own
        handler, connected first: learn the peer it linked, or link a pad it no longer does."""
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        peers = self._relinks.setdefault(element, [])
        peer = pad.get_peer()
        if peer is not None:
            if peer not in peers:
                peers.append(peer)
            return
        for peer in peers:  # a peer gone from the pipeline fails the link
            if not peer.is_linked() and pad.link(peer) == Gst.PadLinkReturn.OK:
                return

    # -- the pipeline's shape: GraphChanged and ModelChanged -- #

    def _inside(self, gobject: Gst.Object) -> bool:
        """Whether an element or pad is (still) in the pipeline."""
        parent = gobject.get_parent()
        while parent is not None and parent != self._host.pipeline:
            parent = parent.get_parent()
        return parent is not None

    def _watch_shape(self, element: Gst.Element) -> None:
        """Follow the pads of an element, on the loop: they come and go on streaming threads."""
        if self._host.loop is None or element in self._shape_ids or not self._inside(element):
            return  # torn down, watched already, or removed again meanwhile
        self._shape_ids[element] = [  # before the walk, so no pad is added unseen meanwhile
            element.connect("pad-added", self._on_pad_signal, self._watch_pad),
            element.connect("pad-removed", self._on_pad_signal, self._unwatch_pad),
        ]
        for pad in _retrying(element.iterate_pads):
            self._watch_pad(pad)

    def _unwatch_shape(self, element: Gst.Element) -> None:
        for gobject in [element, *(o for o in self._shape_ids if o.get_parent() == element)]:
            for handler in self._shape_ids.pop(gobject, ()):
                gobject.disconnect(handler)

    def _watch_pad(self, pad: Gst.Pad) -> None:
        if self._host.loop is None or pad in self._shape_ids or not self._inside(pad):
            return
        self._shape_ids[pad] = [
            pad.connect(signal, self._on_shape_signal)
            for signal in ("notify::caps", "linked", "unlinked")
        ]
        self.shape_changed()

    def _unwatch_pad(self, pad: Gst.Pad) -> None:
        for handler in self._shape_ids.pop(pad, ()):
            pad.disconnect(handler)
        self.shape_changed()

    def _on_pad_signal(
        self, element: Gst.Element, pad: Gst.Pad, callback: Callable[[Gst.Pad], None]
    ) -> None:
        """On whatever thread added or removed the pad: (un)watch it on the loop."""
        self._host._call_soon(callback, pad)

    def _on_shape_signal(self, *args: object) -> None:
        """On whatever thread linked, unlinked or negotiated a pad: nothing more than a wakeup
        of the loop, where the changes of `shape_interval` come together."""
        self._host._call_soon(self.shape_changed)

    def shape_changed(self) -> None:
        """Publish the pipeline's shape in `shape_interval`, with the changes until then."""
        loop = self._host.loop
        if loop is not None and self._shape_flush is None:
            self._shape_flush = loop.call_later(self._host.shape_interval, self._publish_shape)

    def _publish_shape(self) -> None:
        """Publish `GraphChanged` when the graph differs from the last one published, and
        `ModelChanged`, with the commands built again, when the exposed elements or their
        keys do."""
        self._shape_flush = None
        keys = self._host.element_keys()
        renamed = {e: key for key, e in keys.items() if e in self._watched}
        for element, key in renamed.items():  # a namesake came: `q` is `a.q` now
            if self._watched[element] != key:
                self._unwatch(element)
                self._watch(element, key)
        graph = self._model.graph_of(keys)
        if graph != self._graph_seen:
            self._graph_seen = graph
            self._host._publish(events.GraphChanged(graph))
        exposed = [key for key, e in keys.items() if self._model.is_exposed(e)]
        if exposed != self._exposed_seen:
            self._exposed_seen = exposed
            # the action signals of the elements that came, and none of those gone (or renamed)
            self._model.rebuild()
            self._host._publish(events.ModelChanged(self._host.describe()))

    # -- the commands: CommandsChanged -- #

    def commands_changed(self) -> None:
        """Check the commands once the loop is done with what it runs now: any number of calls
        in a turn make one check. A no-op while detached."""
        loop = self._host.loop
        if loop is not None and self._commands_flush is None:
            self._commands_flush = loop.call_soon(self._host.update_commands)

    def update_commands(self) -> None:
        if self._commands_flush is not None:
            self._commands_flush.cancel()
            self._commands_flush = None
        if self._host.loop is None:
            return
        current = self._host.unavailable()
        if current != self._commands_seen:
            self._commands_seen = current
            self._host._publish(events.CommandsChanged(dict(current)))
