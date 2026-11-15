# type: ignore
import asyncio
import json
import threading
from collections.abc import Awaitable

import pytest

from mixtape import Player, command, prop
from mixtape.events import PropertyChanged
from mixtape.exceptions import (
    InvalidValueError,
    ModelError,
    NotWritableError,
    UnknownNameError,
)

PIPELINE = (
    "videotestsrc name=src ! capsfilter name=cf caps=video/x-raw ! "
    "( queue name=nested ! videoconvert ) ! fakesink name=sink"
)


def bounded(awaitable: Awaitable):
    """Fail a regression that hangs instead of hanging the suite"""
    return asyncio.wait_for(awaitable, 5)


async def next_change(stream, path):
    async def find():
        async for event in stream:
            if isinstance(event, PropertyChanged) and event.path == path:
                return event
        raise AssertionError(f"stream ended without a change of {path}")

    return await bounded(find())


def auto_name(player, factory):
    """The generated name of the element made by `factory`"""
    (name,) = [
        e.get_name() for e in player.pipeline.iterate_recurse() if e.get_name().startswith(factory)
    ]
    return name


def playbin_player(Gst):
    pipeline = Gst.Pipeline.new(None)
    pipeline.add(Gst.ElementFactory.make("playbin", "pb"))  # a Pipeline itself, so nested
    return Player(pipeline)


class Controlled(Player):
    bitrate = prop("nested.max-size-buffers", label="Buffers", description="Queue depth")

    @command
    async def greet(self, who: str, times: int = 2) -> str:
        """Greet someone, slowly."""
        await asyncio.sleep(0)
        return who * times

    @command
    def double(self, value: int) -> int:
        return value * 2


# -- exposure -- #


def test_default_exposure_is_the_named_elements_including_nested_ones(Gst):
    player = Player.from_description(PIPELINE)
    spec = player.describe()
    assert set(spec.elements) == {"src", "cf", "nested", "sink"}  # nested is inside a bin
    assert {"state", "position", "duration"} == set(spec.properties)
    assert {"play", "pause", "stop", "eos", "seek"} <= set(spec.commands)
    assert all(c.builtin for c in spec.commands.values())

    everything_player = Player.from_description(PIPELINE, expose_all=True)
    everything = everything_player.describe()
    assert set(everything.elements) > set(spec.elements)
    assert auto_name(everything_player, "videoconvert") in everything.elements


def test_schemas_of_the_supported_value_types(Gst):
    spec = Player.from_description(PIPELINE).describe()
    src, cf, nested, sink = (spec.elements[n].properties for n in ("src", "cf", "nested", "sink"))

    pattern = src["pattern"].value_schema
    assert pattern["type"] == "string" and {"smpte", "snow"} <= set(pattern["enum"])
    assert pattern["default"] == "smpte"
    assert src["is-live"].value_schema["type"] == "boolean"
    depth = nested["max-size-buffers"]
    assert depth.value_schema["type"] == "integer"
    assert (depth.value_schema["minimum"], depth.value_schema["maximum"]) == (0, 2**32 - 1)
    assert depth.writable and depth.readable and depth.path == "nested.max-size-buffers"
    assert cf["caps"].value_schema["type"] == "string"
    assert cf["caps"].value_schema["format"] == "gst-caps"
    assert not nested["current-level-buffers"].writable
    assert "name" not in src
    assert "name" not in sink and "parent" not in sink  # owned by GstObject
    assert "last-sample" not in sink  # an object


def test_flags_schema_and_values(Gst):
    player = playbin_player(Gst)
    schema = player.describe().elements["pb"].properties["flags"].value_schema
    assert schema["type"] == "array" and "audio" in schema["items"]["enum"]
    assert {"audio", "video"} <= set(player.get("pb.flags"))


def test_the_spec_serialises_to_plain_json(Gst):
    player = Controlled.from_description(PIPELINE)
    spec = player.describe()
    data = json.loads(spec.model_dump_json(by_alias=True))
    assert data == spec.model_dump(mode="json", by_alias=True)
    assert data["elements"]["src"]["properties"]["pattern"]["schema"]["type"] == "string"
    assert data["properties"]["bitrate"]["target"] == "nested.max-size-buffers"


# -- set and get -- #


@pytest.mark.asyncio
async def test_set_and_get_by_nick_string_and_caps(Gst):
    player = Player.from_description(PIPELINE)
    await player.set("src.pattern", "snow")
    assert player.get("src.pattern") == "snow"
    await player.set("nested.max-size-buffers", "1500")
    assert player.get("nested.max-size-buffers") == 1500
    await player.set("cf.caps", "video/x-raw,width=320")
    assert player.get("cf.caps") == "video/x-raw, width=(int)320"


@pytest.mark.asyncio
async def test_flags_accept_a_list_or_a_plus_string(Gst):
    player = playbin_player(Gst)
    await player.set("pb.flags", "video+audio")
    assert set(player.get("pb.flags")) == {"video", "audio"}
    await player.set("pb.flags", ["video"])
    assert player.get("pb.flags") == ["video"]


@pytest.mark.asyncio
async def test_out_of_range_raises_and_keeps_the_old_value(Gst):
    player = Player.from_description(PIPELINE)
    await player.set("nested.max-size-buffers", 7)
    for bad in (2**40, -1, "abc"):
        with pytest.raises(InvalidValueError) as info:
            await player.set("nested.max-size-buffers", bad)
        assert info.value.errors
        assert "\n" not in str(info.value)  # one line, for a CLI or an HTTP detail
        assert player.get("nested.max-size-buffers") == 7
    with pytest.raises(InvalidValueError):
        await player.set("cf.caps", "not caps!!")
    with pytest.raises(InvalidValueError):
        await player.set("src.pattern", "nope")


@pytest.mark.asyncio
async def test_unknown_unexposed_and_read_only_paths(Gst):
    player = Player.from_description(PIPELINE)
    auto = auto_name(player, "videoconvert")
    for path in ("nope", "src.nope", "nope.pattern", f"{auto}.qos", "src.name"):
        with pytest.raises(UnknownNameError):
            player.get(path)
        with pytest.raises(UnknownNameError):
            await player.set(path, 1)
    for path in ("nested.current-level-buffers", "state"):
        with pytest.raises(NotWritableError):
            await player.set(path, 1)
    assert issubclass(UnknownNameError, ModelError)

    player = Player.from_description(PIPELINE, expose_all=True)
    auto = auto_name(player, "videoconvert")
    await player.set(f"{auto}.qos", False)
    assert player.get(f"{auto}.qos") is False


# -- declarations -- #


@pytest.mark.asyncio
async def test_prop_and_command_declarations(Gst):
    player = Controlled.from_description(PIPELINE)
    spec = player.describe()
    alias = spec.properties["bitrate"]
    assert alias.value_schema["title"] == "Buffers"
    assert (alias.element, alias.property) == ("nested", "max-size-buffers")
    assert alias.description == "Queue depth"
    greet = spec.commands["greet"]
    assert not greet.builtin and greet.description == "Greet someone, slowly."
    assert greet.parameters["required"] == ["who"]
    assert greet.parameters["properties"]["times"]["default"] == 2
    assert greet.result == {"type": "string"}
    assert spec.commands["double"].result == {"type": "integer"}

    assert await player.invoke("double", value="3") == 6
    assert await player.invoke("greet", who="ab", times="2") == "abab"
    for name, args in (
        ("double", {"value": "x"}),
        ("double", {}),
        ("double", {"value": 1, "z": 2}),
    ):
        with pytest.raises(InvalidValueError):
            await player.invoke(name, **args)
    with pytest.raises(UnknownNameError):
        await player.invoke("nope")

    await player.set("bitrate", 9)
    assert player.get("nested.max-size-buffers") == player.get("bitrate") == player.bitrate == 9
    with pytest.raises(AttributeError, match="player.set"):
        player.bitrate = 1
    assert isinstance(Controlled.bitrate.target, str)  # on the class: the declaration


def test_a_typo_in_an_alias_fails_at_construction(Gst):
    class Typo(Player):
        speed = prop("nested.max-size-bufers")

    with pytest.raises(UnknownNameError):
        Typo.from_description(PIPELINE)


def test_bad_declarations_fail_when_the_class_is_defined():
    with pytest.raises(TypeError, match="type hint"):

        class Untyped(Player):
            @command
            def go(self, speed): ...

    with pytest.raises(TypeError, match="clashes"):

        class Clash(Player):
            state = prop("src.pattern")


# -- built-ins -- #


@pytest.mark.asyncio
async def test_builtin_play_and_stop_commands_move_the_state(Gst):
    async with Player.from_description("videotestsrc is-live=true ! fakesink") as player:
        assert player.get("state") == "null"
        assert player.describe().commands["play"].result is None
        assert await bounded(player.invoke("play")) is None  # not a Gst.StateChangeReturn
        assert player.get("state") == "playing"
        assert player.get("position") is None or player.get("position") >= 0
        await bounded(player.invoke("stop"))
        assert player.get("state") == "null"
        with pytest.raises(InvalidValueError):
            await player.invoke("seek", position=-1)


# -- change events -- #


@pytest.mark.asyncio
async def test_property_changed_from_another_thread_arrives_on_the_loop(Gst):
    async with Player.from_description(PIPELINE) as player:
        stream = player.events()
        src = player.pipeline.get_by_name("src")
        thread = threading.Thread(target=src.set_property, args=("pattern", "ball"))
        thread.start()
        event = await next_change(stream, "src.pattern")
        thread.join()
        assert (event.element, event.property, event.value) == ("src", "pattern", "ball")


@pytest.mark.asyncio
async def test_property_changed_after_set_and_no_handlers_after_teardown(Gst):
    player = Player.from_description(PIPELINE)
    async with player:
        stream = player.events()
        await player.set("nested.max-size-buffers", 42)
        assert (await next_change(stream, "nested.max-size-buffers")).value == 42
    assert not player._notify_ids
    player.pipeline.get_by_name("src").set_property("pattern", "snow")  # nobody listens
