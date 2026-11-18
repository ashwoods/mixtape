"""Values as JSON, as the HTTP API and the command line's JSON output write them. This needs
neither GStreamer nor the http extra: `mixtape perf compare --json` prints its errors with it."""

import json
import math
from typing import Any

from pydantic_core import to_jsonable_python


def _finite(value: Any) -> Any:
    """`value`, made of what `to_jsonable_python` returns, with every NaN and infinity in it as
    None: JSON has no such numbers."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def jsonable(value: Any) -> Any:
    """`value` as data JSON can carry, with NaN and infinities as None, as the properties of
    REST come (pydantic's way). A value it cannot carry (an object) raises ValueError."""
    return _finite(to_jsonable_python(value, by_alias=True))


def encode_json(message: Any) -> str:
    """The JSON text of `jsonable(message)`. A value JSON cannot carry (an object) raises
    ValueError."""
    return json.dumps(jsonable(message), allow_nan=False)
