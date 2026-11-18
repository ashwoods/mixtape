from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # only an annotation: the codes are importable without PyGObject
    from gi.repository import GLib


class MixTapeError(Exception):
    """Mixtape exception base clase"""

    code: ClassVar[str] = "internal"
    """The stable name of the error, as the command line's and the HTTP API's JSON errors
    give it: one per class"""


class PlayerNotConfigured(MixTapeError):
    """The player is not attached to the running asyncio loop"""

    code = "internal"


class PlayerDescriptionError(MixTapeError):
    """A pipeline description could not be parsed"""

    code = "invalid_description"


class PlayerSetStateError(MixTapeError):
    """Setting the pipeline state has failed"""

    code = "state_change_failed"


class PlayerPipelineError(MixTapeError):
    """Error originating from Gst Pipeline"""

    code = "pipeline_error"

    def __init__(
        self,
        source: str,
        message: str,
        debug: str | None = None,
        gerror: "GLib.Error | None" = None,
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

    code = "model_error"


class UnknownNameError(ModelError):
    """A property path or command name does not exist, or is not exposed"""

    code = "unknown_name"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint
        """The close names, `did you mean 'a' or 'b'?`, when there are some"""


class InvalidValueError(ModelError):
    """A value or an argument was rejected, and nothing was changed"""

    code = "invalid_value"

    def __init__(self, message: str, errors: list[Any] | None = None) -> None:
        super().__init__(message)
        self.errors: list[Any] = errors or []
        """Pydantic's error list when validation failed, else empty"""


class NotReadableError(ModelError):
    """The property cannot be read"""

    code = "not_readable"


class NotWritableError(ModelError):
    """The property cannot be written: it is read-only or construct-only"""

    code = "not_writable"


class FeatureError(ModelError):
    """A feature could not be resolved, configured or enabled"""

    code = "feature_error"


class CommandError(ModelError):
    """A command could not do what was asked, or cannot run now"""

    code = "command_failed"
