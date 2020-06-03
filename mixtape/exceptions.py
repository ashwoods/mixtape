class MixTapeError(Exception):
    """Mixtape exception base clase"""


class PlayerNotConfigured(MixTapeError):
    """Player setup not completed"""


class PlayerAlreadyConfigured(MixTapeError):
    """Player has already been setup"""


class PlayerSetStateError(MixTapeError):
    """Setting the pipeline state has failed"""


class PlayerPipelineError(MixTapeError):
    """Error originating from Gst Pipeline"""
