"""A player with an alias of an element property and a command: `mixtape inspect examples/demo.py:Demo`.

mixtape inspect examples/demo.py:Demo
"""

# --8<-- [start:player]
from mixtape import Player, command, prop


class Demo(Player):
    description = "audiotestsrc num-buffers=100 ! volume name=vol ! fakesink"

    gain = prop("vol.volume", description="Output level, 1 is unchanged")

    @command
    def shout(self, word: str, times: int = 2) -> str:
        """Repeat a word."""
        return word * times


# --8<-- [end:player]
