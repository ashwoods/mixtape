import warnings

from mixtape import Player


class AsyncPlayer(Player):
    def __init_subclass__(cls) -> None:
        warnings.warn("Class has been renamed Player", DeprecationWarning, 2)
