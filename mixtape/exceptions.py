from typing import Any

from gi.repository import GLib


class MixTapeError(Exception):
    """Mixtape exception base clase"""


class PlayerNotConfigured(MixTapeError):
    """The player is not attached to the running asyncio loop"""


class PlayerDescriptionError(MixTapeError):
    """A pipeline description could not be parsed"""


class PlayerSetStateError(MixTapeError):
    """Setting the pipeline state has failed"""


class PlayerPipelineError(MixTapeError):
    """Error originating from Gst Pipeline"""

    def __init__(
        self,
        source: str,
        message: str,
        debug: str | None = None,
        gerror: GLib.Error | None = None,
    ) -> None:
        super().__init__(source, message, debug)
        self.source = source
        """Name of the element that posted the error"""
        self.message = message
        self.debug = debug
        self.gerror = gerror
        """The `GLib.Error` of the bus message"""

    def __str__(self) -> str:
        text = f"{self.source}: {self.message}"
        return f"{text} ({self.debug})" if self.debug else text


class ModelError(MixTapeError):
    """Base of the errors of the declarative model: properties and commands"""


class UnknownNameError(ModelError):
    """A property path or command name does not exist, or is not exposed"""


class InvalidValueError(ModelError):
    """A value or an argument was rejected, and nothing was changed"""

    def __init__(self, message: str, errors: list[Any] | None = None) -> None:
        super().__init__(message)
        self.errors: list[Any] = errors or []
        """Pydantic's error list when validation failed, else empty"""


class NotReadableError(ModelError):
    """The property cannot be read"""


class NotWritableError(ModelError):
    """The property cannot be written: it is read-only or construct-only"""


class FeatureError(ModelError):
    """A feature could not be resolved, configured or enabled"""


class CommandError(ModelError):
    """A built-in command could not do what was asked"""
