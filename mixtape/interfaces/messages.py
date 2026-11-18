"""The JSON messages of a player's events, which the `/api/events` WebSocket sends and
`mixtape run --events jsonl` prints: a snapshot of the model, the values and the graph, then
one message per event. Their shapes are listed in `interfaces.http.ws`. This needs no http
extra. The messages become JSON text with `encoding.encode_json`."""

from typing import Any

from .. import events
from ..exceptions import UnknownNameError
from ..spec import PlayerSpec
from ..player import Player
from .values import all_values, read_values


def encode_event(event: events.Event) -> dict[str, Any] | None:
    """The JSON message of an event, None for those the page has no use for."""
    match event:
        case events.StateChanged(old=old, new=new, pending=pending):
            return {
                "type": "state",
                "old": old.value_nick,
                "new": new.value_nick,
                "pending": pending.value_nick,
            }
        case events.PropertyChanged(path=path, element=element, property=prop, value=value):
            return {
                "type": "property",
                "path": path,
                "element": element,
                "property": prop,
                "value": value,
            }
        case events.Eos():
            return {"type": "eos"}
        case events.Error(error=error):
            return {
                "type": "error",
                "source": error.source,
                "message": error.message,
                "debug": error.debug,
            }
        case events.Warning(source=source, message=message, debug=debug):
            return {"type": "warning", "source": source, "message": message, "debug": debug}
        case events.Position(position=position, duration=duration):
            return {"type": "position", "position": position, "duration": duration}
        case events.CommandsChanged(unavailable=unavailable):
            return {"type": "commands", "unavailable": unavailable}
        case events.GraphChanged(graph=graph):
            return {"type": "graph", "graph": graph.model_dump(mode="json")}
        case events.ModelChanged(model=model):
            return {"type": "model", "model": model.model_dump(mode="json")}
    return None  # Message


class EventEncoder:
    """The messages of one subscriber of `player`: `snapshot()` first, then `encode()` for
    each event. A `model` message gets the `values` of the elements it adds."""

    def __init__(self, player: Player) -> None:
        self.player = player
        self._known: set[str] = set()  # the elements whose values the subscriber has

    def snapshot(self) -> list[dict[str, Any]]:
        """The model, the values and the graph, and the error that stopped the pipeline, if
        any: a page may load after it."""
        model = self.player.describe()
        self._known = set(model.elements)
        messages: list[dict[str, Any]] = [
            {"type": "model", "model": model.model_dump(mode="json")},
            {"type": "values", "values": all_values(self.player)},
            {"type": "graph", "graph": self.player.graph().model_dump(mode="json")},
        ]
        error = self.player.error
        if error is not None and (message := encode_event(events.Error(error))) is not None:
            messages.append(message)
        return messages

    def encode(self, event: events.Event) -> dict[str, Any] | None:
        """The message of `event`, None for those that have none."""
        if (message := encode_event(event)) is None:
            return None
        if isinstance(event, events.ModelChanged):
            elements = set(event.model.elements)
            message["values"] = _element_values(self.player, event.model, elements - self._known)
            self._known = elements
        return message


def _element_values(player: Player, model: PlayerSpec, keys: set[str]) -> dict[str, Any]:
    """`{path: value}` of the readable properties of the elements `keys` of `model`."""
    values: dict[str, Any] = {}
    for key in keys:
        paths = [p.path for p in model.elements[key].properties.values() if p.readable]
        try:
            values |= read_values(player, {path: path for path in paths})
        except UnknownNameError:
            continue  # gone again since the model, or renamed by a namesake: a later one says
    return values
