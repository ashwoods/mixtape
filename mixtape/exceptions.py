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
