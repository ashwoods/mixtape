"""Player subclasses for the command line tests: `mixtape run tests.players:Demo`."""

from typing import Any

import pytest

from mixtape import Feature, Player, command, features, prop


class Demo(Player):
    description = "videotestsrc name=src num-buffers=5 ! fakesink name=sink"

    pattern = prop("src.pattern", description="The test pattern")

    @command
    def shout(self, word: str, times: int = 2) -> str:
        """Repeat a word."""
        return word * times


class Feat(Feature):
    """A feature that records its lifecycle, with an option, a command and a changing property."""

    name = "feat"

    def __init__(self, step: float = 1.0) -> None:
        self.step = step
        self.value = 0.0
        self.calls: list[str] = []

    def prepare(self) -> None:
        self.calls.append("prepare")

    def attach(self, player: Player) -> None:
        self.calls.append(f"attach:{self.player is player}")

    def detach(self, player: Player) -> None:
        self.calls.append(f"detach:{self.player is player}")

    @command
    def bump(self, times: int = 1) -> float:
        """Raise the level."""
        self.value += self.step * times
        self.notify("level")
        return self.value

    @prop
    def level(self) -> float:
        """The current level."""
        return self.value


class _EntryPoint:
    def __init__(self, name: str, obj: Any) -> None:
        self.name, self.obj = name, obj

    def load(self) -> Any:
        return self.obj


def register(monkeypatch: pytest.MonkeyPatch, *classes: Any, **named: Any) -> None:
    """Make `classes` (by their `name`) and `named` the installed features."""
    entries = {c.name: _EntryPoint(c.name, c) for c in classes}
    entries |= {name: _EntryPoint(name, obj) for name, obj in named.items()}
    monkeypatch.setattr(features, "available", lambda: entries)


class NoDescription(Player):
    pass


NOT_A_PLAYER = 42
