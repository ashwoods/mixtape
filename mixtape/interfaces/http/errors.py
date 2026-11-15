"""How the errors of a player become HTTP statuses and WebSocket error replies."""

from typing import Any

from fastapi.encoders import jsonable_encoder

from ...exceptions import (
    CommandError,
    InvalidValueError,
    MixTapeError,
    NotReadableError,
    NotWritableError,
    PlayerPipelineError,
    PlayerSetStateError,
    UnknownNameError,
)


def describe_error(exc: MixTapeError) -> tuple[int, dict[str, Any]]:
    """The status and JSON body (`detail`, and `errors` for an invalid value) of an error."""
    if isinstance(exc, UnknownNameError):
        status = 404
    elif isinstance(exc, InvalidValueError | NotWritableError | NotReadableError):
        status = 422
    elif isinstance(exc, CommandError | PlayerSetStateError):
        status = 409
    else:
        status = 500
    if isinstance(exc, PlayerPipelineError):
        body: dict[str, Any] = {"detail": f"{exc.source}: {exc.message}"}
    else:
        body = {"detail": str(exc)}
    if isinstance(exc, InvalidValueError):
        body["errors"] = jsonable_encoder(exc.errors)
    return status, body
