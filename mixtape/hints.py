"""The `did you mean …?` of an unknown name, from a player's model as JSON: what
`Player.describe().model_dump(mode="json")` and `GET /api/model` return. It needs neither
GStreamer nor the model's classes, so a client of the HTTP API can use it too."""

import difflib
from collections.abc import Iterable, Mapping
from typing import Any, Literal


def property_hint(
    model: Mapping[str, Any], path: str, access: Literal["read", "write"] = "write"
) -> str | None:
    """The closest of the properties of `model` that can be read or written, as `access` says,
    to the unknown `path`; None when none is close."""
    flag = "readable" if access == "read" else "writable"
    elements = model.get("elements", {})
    key, _, name = path.rpartition(".")
    if (element := elements.get(key)) is not None:  # its properties, by their own names
        names = [n for n, p in element["properties"].items() if p[flag]]
        return did_you_mean(name, names, prefix=f"{key}.")
    # a typo in the element's key, or one of the player's own properties
    props = [*model.get("properties", {}).values()]
    props += [p for e in elements.values() for p in e["properties"].values()]
    return did_you_mean(path, [p["path"] for p in props if p[flag]])


def command_hint(model: Mapping[str, Any], name: str) -> str | None:
    """The closest of the commands of `model` to the unknown `name`; None when none is close."""
    return did_you_mean(name, model.get("commands", {}))


def did_you_mean(name: str, names: Iterable[str], *, prefix: str = "") -> str | None:
    """`did you mean 'a' or 'b'?` with the (at most three) of `names` closest to the unknown
    `name`, each after `prefix`; None when none is close."""
    close = difflib.get_close_matches(name, list(names), n=3)
    return f"did you mean {' or '.join(repr(prefix + c) for c in close)}?" if close else None
