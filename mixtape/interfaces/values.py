"""Reading and writing property values for the REST and WebSocket interfaces and the command
line's events."""

from collections.abc import Callable, Mapping
from typing import Any

from ..exceptions import NotReadableError
from ..player import Player


def read_values(player: Player, paths: Mapping[str, str]) -> dict[str, Any]:
    """`{key: value}` for the `{key: path}` given, leaving out the write-only ones."""
    values: dict[str, Any] = {}
    for key, path in paths.items():
        try:
            values[key] = player.get(path)
        except NotReadableError:
            continue  # a write-only property has no value to report
    return values


def all_values(player: Player) -> dict[str, Any]:
    """`{path: value}` of every readable property: the built-ins, the aliases and those of
    every exposed element."""
    spec = player.describe()
    props = list(spec.properties.values())
    for element in spec.elements.values():
        props.extend(element.properties.values())
    return read_values(player, {p.path: p.path for p in props if p.readable})


async def set_values(
    player: Player, values: Mapping[str, Any], path_of: Callable[[str], str] = str
) -> None:
    """Set the values in order, stopping at the first error. Those before it stay set.
    `path_of` turns a key into the path to set, and may raise `UnknownNameError`."""
    for key, value in values.items():
        await player.set(path_of(key), value)
