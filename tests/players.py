"""Player subclasses for the command line tests: `mixtape run tests.players:Demo`."""

from mixtape import Player, command, prop


class Demo(Player):
    description = "videotestsrc name=src num-buffers=5 ! fakesink name=sink"

    pattern = prop("src.pattern", description="The test pattern")

    @command
    def shout(self, word: str, times: int = 2) -> str:
        """Repeat a word."""
        return word * times


class NoDescription(Player):
    pass


NOT_A_PLAYER = 42
