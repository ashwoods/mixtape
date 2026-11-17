import asyncio
from typing import Any

import pytest

import mixtape.player
from mixtape import Feature, Player, prop
from mixtape.events import PropertyChanged
from mixtape.exceptions import FeatureError, NotWritableError, UnknownNameError
from mixtape.features import resolve
from tests.players import Feat, register

PIPELINE = "videotestsrc name=src ! fakesink name=sink"


# -- resolving -- #


def test_resolve_a_string_with_options(monkeypatch: pytest.MonkeyPatch) -> None:
    register(monkeypatch, Feat)
    feature = resolve("feat,step=0.5")  # a string from the command line is a float
    assert isinstance(feature, Feat) and feature.step == 0.5
    assert isinstance(default := resolve("feat"), Feat) and default.step == 1.0


def test_resolve_a_class_and_an_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    register(monkeypatch)  # neither needs the registry
    assert isinstance(resolve(Feat), Feat)
    feature = Feat(step=3)
    assert resolve(feature) is feature


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("feat,step=x", "step: Input should be a valid number"),
        ("feat,nope=1", r"Feature 'feat' has no option 'nope'; its options: step \(number\)$"),
        ("feat,step", "not KEY=VALUE"),
        ("feat,=1", "not KEY=VALUE"),
        ("nofeat", "available: feat, other"),
        ("other", "not a Feature subclass named 'other'"),
    ],
)
def test_resolve_rejects(spec: str, message: str, monkeypatch: pytest.MonkeyPatch) -> None:
    register(monkeypatch, Feat, other=Player)
    with pytest.raises(FeatureError, match=message) as caught:
        resolve(spec)
    assert len(str(caught.value).splitlines()) == 1


def test_a_feature_class_is_checked_when_defined() -> None:
    with pytest.raises(TypeError, match="name must match"):

        class Nameless(Feature):
            pass

    with pytest.raises(TypeError, match="computed properties only"):

        class Aliasing(Feature):
            name = "aliasing"
            depth = prop("q.max-size-buffers")

    with pytest.raises(TypeError, match="needs a type hint"):

        class Untyped(Feature):
            name = "untyped"

            def __init__(self, step) -> None:  # type: ignore[no-untyped-def]
                pass

    with pytest.raises(TypeError, match="not instances"):

        class Shared(Player):
            features = (Feat(),)  # type: ignore[assignment]


def test_a_getter_needs_a_return_hint_and_no_builtin_name() -> None:
    with pytest.raises(TypeError, match="needs a return type hint"):

        class Untyped(Player):
            @prop
            def fps(self):  # type: ignore[no-untyped-def]
                return 1.0

    with pytest.raises(TypeError, match="clashes with a built-in"):

        class Clash(Player):
            @prop
            def state(self) -> str:
                return "x"


# -- the model -- #


@pytest.mark.asyncio
async def test_the_model_lists_the_commands_and_properties_of_a_feature(Gst: Any) -> None:
    player = Player.from_description(PIPELINE, features=[Feat(step=2)])
    spec = player.describe()
    command_spec = spec.commands["feat.bump"]
    assert (command_spec.name, command_spec.builtin) == ("feat.bump", False)
    assert command_spec.description == "Raise the level."
    assert command_spec.parameters["properties"]["times"]["type"] == "integer"
    assert command_spec.result == {"type": "number"}
    level = spec.properties["feat.level"]
    assert (level.path, level.builtin, level.writable, level.readable) == (
        "feat.level",
        False,
        False,
        True,
    )
    assert level.value_schema["type"] == "number" and level.description == "The current level."
    assert all(spec.properties[name].builtin for name in ("state", "position", "duration"))
    assert not command_spec.builtin and spec.commands["play"].builtin

    assert player.get("feat.level") == 0  # no setup needed
    assert await player.invoke("feat.bump", times=2) == 4
    assert player.get("feat.level") == 4
    with pytest.raises(NotWritableError):
        await player.set("feat.level", 1)
    with pytest.raises(UnknownNameError):
        await player.invoke("feat.nope")
    with pytest.raises(UnknownNameError):
        player.notify("src.pattern")  # not a computed property


@pytest.mark.asyncio
async def test_a_player_subclass_has_computed_properties(Gst: Any) -> None:
    class Clocked(Player):
        @prop
        def uptime(self) -> float:
            """Seconds since the start.

            More text."""
            return 1.5

        @prop
        def tags(self) -> dict[str, list[int]]:
            return {"a": [1]}

    player = Clocked.from_description(PIPELINE)
    uptime = player.describe().properties["uptime"]
    assert (uptime.writable, uptime.builtin, uptime.description) == (
        False,
        False,
        "Seconds since the start.",
    )
    assert player.get("uptime") == 1.5 and player.uptime == 1.5
    assert player.get("tags") == {"a": [1]}
    with pytest.raises(NotWritableError):
        await player.set("uptime", 2)
    with pytest.raises(AttributeError):
        player.uptime = 2


# -- the lifecycle -- #


@pytest.mark.asyncio
async def test_attach_detach_and_notify(Gst: Any) -> None:
    feature = Feat()
    player = Player.from_description(PIPELINE, features=[feature])
    assert feature.player is None and feature.calls == ["prepare"]
    async with player:
        await player.ready()  # setup again: no second attach
        assert feature.calls == ["prepare", "attach:True"] and feature.player is player
        stream = player.events()
        await player.invoke("feat.bump")
        event = await asyncio.wait_for(anext(stream), 5)
        assert event == PropertyChanged("feat.level", None, None, 1.0)
    player.teardown()  # once more
    assert feature.calls == ["prepare", "attach:True", "detach:True"] and feature.player is None
    feature.notify("level")  # detached: nothing happens
    async with player:  # re-usable
        assert feature.calls[-1] == "attach:True"


@pytest.mark.asyncio
async def test_prepare_runs_before_gstreamer_is_initialised(
    Gst: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature = Feat()
    init = mixtape.player.ensure_init

    def recording_init() -> None:
        feature.calls.append("init")
        init()

    monkeypatch.setattr(mixtape.player, "ensure_init", recording_init)
    Player.from_description(PIPELINE, features=[feature])
    assert feature.calls[:2] == ["prepare", "init"] and feature.calls.count("prepare") == 1


# -- enabling -- #


def test_requires_and_the_name_of_an_element(Gst: Any) -> None:
    class Needy(Feat):
        name = "needy"
        requires = ("videotestsrc", "nosuchelement", "nosuchtracer")

    with pytest.raises(FeatureError, match="'needy'.*'nosuchelement', 'nosuchtracer'"):
        Player.from_description(PIPELINE, features=[Needy()])
    with pytest.raises(FeatureError, match="has the name of an element"):
        Player.from_description("videotestsrc name=feat ! fakesink", features=[Feat()])


def test_class_attribute_features_and_explicit_ones(
    Gst: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, Feat)

    class Featured(Player):
        features = ("feat,step=2",)

    steps: list[float] = []
    for features in ((), [Feat(step=5)]):  # the class's, then the explicit one instead
        player = Featured.from_description(PIPELINE, features=features)
        assert list(player.enabled_features) == ["feat"]
        feature = player.enabled_features["feat"]
        assert isinstance(feature, Feat)
        steps.append(feature.step)
    assert steps == [2, 5]
    assert list(Player.from_description(PIPELINE).enabled_features) == []
    with pytest.raises(FeatureError, match="enabled twice"):
        Player.from_description(PIPELINE, features=[Feat(), "feat"])
