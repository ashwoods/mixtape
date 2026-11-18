# type: ignore
import asyncio
import json
import threading
from collections.abc import Awaitable

import pytest
from pydantic import BaseModel

from mixtape import Player, command, prop
from mixtape.events import GraphChanged, ModelChanged, PropertyChanged
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
    assert {"type": "string"} in cf["caps"].value_schema["anyOf"]  # or null, unset
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


@pytest.mark.asyncio
async def test_a_pipeline_element_at_the_top_is_exposed_too(Gst):
    class Radio(Player):
        muted = prop("pb.mute")

    player = Radio.from_description("playbin name=pb")  # playbin is the pipeline itself
    assert "pb" in player.describe().elements
    await player.set("pb.volume", 0.5)
    assert player.get("pb.volume") == 0.5
    stream = player.events()
    await player.set("muted", True)
    assert player.get("pb.mute") is True
    assert (await next_change(stream, "pb.mute")).value is True
    player.teardown()
    assert "pipeline0" not in Player.from_description("fakesink").describe().elements


def test_playbins_internal_playsink_is_not_user_named(Gst):
    description = "playbin name=pb uri=file:///x"
    assert set(Player.from_description(description).describe().elements) == {"pb"}
    everything = Player.from_description(description, expose_all=True).describe()
    assert {"pb", "playsink"} <= set(everything.elements)


def test_plain_bins_inside_an_element_are_not_the_users(Gst):
    # playsink builds plain bins (vbin) with named children once it plays
    player = Player.from_description("playbin name=pb uri=file:///x")
    playsink = player.pipeline.get_by_name("playsink")
    vbin = Gst.Bin.new("vbin")
    vbin.add(Gst.ElementFactory.make("queue", "vqueue"))
    playsink.add(vbin)
    assert set(player.describe().elements) == {"pb"}


@pytest.mark.asyncio
async def test_the_graph_links_elements_through_bins(Gst):
    player = Player.from_description("fakesrc ! bin.( name=a queue name=q ) ! fakesink name=k")
    async with player:
        await player.pause()
        graph = player.graph()
    assert graph.nodes["a"].bin and graph.nodes["q"].parent == "a" and graph.nodes["q"].exposed
    (source,) = [key for key, node in graph.nodes.items() if node.factory == "fakesrc"]
    assert not graph.nodes[source].exposed
    assert [(link.source, link.sink) for link in graph.links] == [(source, "q"), ("q", "k")]
    assert graph.links[0].source_pad == "src" and graph.links[0].sink_pad == "sink"


COLLIDING = (
    "bin.( name=a queue name=q ! fakesink ) bin.( name=b queue name=q ! fakesink ) "
    "bin.( name=c queue name=only ! fakesink )"
)


@pytest.mark.asyncio
async def test_same_named_elements_are_keyed_by_their_path(Gst):
    player = Player.from_description(COLLIDING)
    spec = player.describe()
    assert {"a.q", "b.q", "only"} <= set(spec.elements)  # a unique name stays plain
    assert "q" not in spec.elements
    assert spec.elements["a.q"].name == "a.q"
    prop_spec = spec.elements["b.q"].properties["max-size-buffers"]
    assert (prop_spec.path, prop_spec.element) == ("b.q.max-size-buffers", "b.q")

    await player.set("a.q.max-size-buffers", 7)
    assert player.get("a.q.max-size-buffers") == 7
    assert player.get("b.q.max-size-buffers") == 200
    with pytest.raises(UnknownNameError, match=r"a\.q.*b\.q"):
        player.get("q.max-size-buffers")

    # the keys do not change with expose_all
    everything = Player.from_description(COLLIDING, expose_all=True).describe()
    assert {"a.q", "b.q", "only"} <= set(everything.elements) and "q" not in everything.elements

    class Aliased(Player):
        depth = prop("b.q.max-size-buffers")

    aliased = Aliased.from_description(COLLIDING)
    await aliased.set("depth", 3)
    assert aliased.get("b.q.max-size-buffers") == 3
    assert aliased.describe().properties["depth"].element == "b.q"


@pytest.mark.asyncio
async def test_property_changed_of_a_colliding_element_carries_its_key(Gst):
    async with Player.from_description(COLLIDING) as player:
        stream = player.events()
        await player.set("a.q.max-size-buffers", 9)
        event = await next_change(stream, "a.q.max-size-buffers")
        assert (event.element, event.property, event.value) == ("a.q", "max-size-buffers", 9)


@pytest.mark.asyncio
async def test_unset_caps_read_as_none(Gst):
    player = Player.from_description("appsrc name=a ! fakesink")
    assert player.get("a.caps") is None
    await player.set("a.caps", "video/x-raw")
    assert player.get("a.caps") == "video/x-raw"
    await player.set("a.caps", None)
    assert player.get("a.caps") is None


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

    class Named(Player):
        @command
        def hello(self, name: str) -> str:
            return f"hi {name}"

    named = Named.from_description(PIPELINE)
    assert await named.invoke("hello", name="x") == "hi x"  # not invoke's own `name`

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


class Point(BaseModel):
    x: int
    y: int = 0


class Segment(BaseModel):
    start: Point
    end: Point | None = None


def test_a_command_can_say_when_it_can_run(Gst):
    def check(self):
        return True

    class Gated(Player):
        @command
        def always(self) -> None: ...

        @command(when=check)
        def sometimes(self) -> None: ...

    commands = Gated._declarations().commands
    assert commands["always"].when is None
    assert commands["sometimes"].when is check


@pytest.mark.asyncio
async def test_command_schemas_have_their_models_inlined(Gst):
    class Draw(Player):
        @command
        def go(self, where: Point, path: list[Segment], hint: Point = Point(x=1)) -> Segment:  # noqa: B008
            """Go."""
            return Segment(start=where)

    spec = Draw.from_description("fakesink").describe().commands["go"]
    for schema in (spec.parameters, spec.result):
        assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    assert spec.parameters["properties"]["where"]["properties"]["x"] == {
        "title": "X",
        "type": "integer",
    }
    hint = spec.parameters["properties"]["hint"]
    assert hint["default"] == {"x": 1, "y": 0} and hint["properties"]["y"]["default"] == 0
    assert spec.result["properties"]["start"]["properties"]["y"]["default"] == 0

    player = Draw.from_description("fakesink")
    result = await player.invoke("go", where={"x": "2"}, path=[])
    assert result.start == Point(x=2)
    with pytest.raises(InvalidValueError):
        await player.invoke("go", where={"x": "a"}, path=[])


def test_a_recursive_model_fails_when_the_class_is_defined():
    class Tree(BaseModel):
        children: list["Tree"] = []

    class Ping(BaseModel):
        pong: "Pong | None" = None

    class Pong(BaseModel):
        ping: Ping | None = None

    Ping.model_rebuild()
    with pytest.raises(TypeError, match="grow.*Tree"):

        class Growing(Player):
            @command
            def grow(self, tree: Tree) -> None: ...

    with pytest.raises(TypeError, match="show.*Ping"):

        class Showing(Player):
            @command
            def show(self) -> Ping: ...


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
async def test_property_changed_of_an_element_added_after_setup(Gst):
    async with Player.from_description(PIPELINE) as player:
        stream = player.events()
        late = Gst.ElementFactory.make("identity", "late")
        player.pipeline.add(late)
        await asyncio.sleep(0)  # deep-element-added hands the element to the loop
        threading.Thread(target=late.set_property, args=("sleep-time", 3)).start()
        assert (await next_change(stream, "late.sleep-time")).value == 3
        player.pipeline.remove(late)
        await asyncio.sleep(0)
        assert all(element != late for element, _ in player._notify_ids)


@pytest.mark.asyncio
async def test_property_changed_after_set_and_no_handlers_after_teardown(Gst):
    player = Player.from_description(PIPELINE)
    async with player:
        stream = player.events()
        await player.set("nested.max-size-buffers", 42)
        assert (await next_change(stream, "nested.max-size-buffers")).value == 42
    assert not player._notify_ids
    assert not player._shape_ids
    player.pipeline.get_by_name("src").set_property("pattern", "snow")  # nobody listens


# -- shape events -- #

DECODED = "audiotestsrc num-buffers=200 ! wavenc ! decodebin ! fakesink name=sink"


async def next_event(stream, kind, accept=lambda event: True):
    async def find():
        async for event in stream:
            if isinstance(event, kind) and accept(event):
                return event
        raise AssertionError(f"stream ended without a {kind.__name__}")

    return await bounded(find())


@pytest.mark.asyncio
async def test_the_graph_and_model_follow_the_elements_decodebin_adds(Gst):
    async with Player.from_description(DECODED, expose_all=True) as player:
        stream = player.events()
        await bounded(player.pause())

        def parsed(event):  # wavparse, linked to the sink through decodebin, with caps
            nodes = event.graph.nodes
            return any(
                nodes[link.source].factory == "wavparse" and link.sink == "sink" and link.caps
                for link in event.graph.links
            )

        graph = (await next_event(stream, GraphChanged, parsed)).graph
        (key,) = [key for key, node in graph.nodes.items() if node.factory == "wavparse"]
        model = await next_event(stream, ModelChanged, lambda e: key in e.model.elements)
        assert model.model == player.describe()


@pytest.mark.asyncio
async def test_changes_together_come_as_one_graph(Gst):
    async with Player.from_description(PIPELINE) as player:
        stream = player.events()
        for i in range(5):
            player.pipeline.add(Gst.ElementFactory.make("identity", f"late{i}"))
        await asyncio.sleep(player.shape_interval * 4)
        stream.close()
        changes = [event async for event in stream if isinstance(event, GraphChanged)]
        assert len(changes) == 1
        assert {f"late{i}" for i in range(5)} <= set(changes[0].graph.nodes)


@pytest.mark.asyncio
async def test_no_shape_handlers_stay_on_a_removed_element(Gst):
    async with Player.from_description(PIPELINE) as player:
        late = Gst.ElementFactory.make("identity", "late")
        player.pipeline.add(late)
        await asyncio.sleep(0)  # deep-element-added hands the element to the loop
        assert late in player._shape_ids and late.get_static_pad("src") in player._shape_ids
        player.pipeline.remove(late)
        await asyncio.sleep(0)
        assert not [o for o in player._shape_ids if o == late or o.get_parent() == late]


@pytest.mark.asyncio
async def test_an_element_added_later_brings_its_action_signals_and_takes_them(Gst):
    async with Player.from_description("fakesrc ! fakesink", expose_all=True) as player:
        stream = player.events()
        player.pipeline.add(Gst.ElementFactory.make("appsrc", "late"))
        model = (await next_event(stream, ModelChanged)).model
        assert "late.end-of-stream" in model.commands
        assert (await bounded(player.invoke("late.end-of-stream"))) == "ok"
        player.pipeline.remove(player.pipeline.get_by_name("late"))
        model = (await next_event(stream, ModelChanged)).model
        assert "late.end-of-stream" not in model.commands
        assert "late.end-of-stream" not in player._commands  # its emit keeps no element alive


@pytest.mark.asyncio
async def test_a_namesake_renames_an_element_and_its_changes_follow(Gst):
    async with Player.from_description("bin.( name=a queue name=q ! fakesink )") as player:
        stream = player.events()
        assert "q" in player.describe().elements
        b = Gst.Bin.new("b")
        b.add(Gst.ElementFactory.make("queue", "q"))
        player.pipeline.add(b)
        model = (await next_event(stream, ModelChanged)).model
        assert {"a.q", "b.q"} <= set(model.elements) and "q" not in model.elements
        a_q = player.pipeline.get_by_name("a").get_by_name("q")
        threading.Thread(target=a_q.set_property, args=("max-size-buffers", 7)).start()
        assert (await next_change(stream, "a.q.max-size-buffers")).value == 7


@pytest.mark.asyncio
async def test_a_summary_is_the_first_paragraph_on_one_line(Gst):
    class Wrapped(Player):
        @command
        async def stop_soon(self) -> None:
            """Send an EOS event and wait for the EOS message it causes, while playing, for up
            to `timeout` seconds.

            A second paragraph is detail, not summary.
            """

        @prop
        def level(self) -> int:
            """The level of the thing,
            as it was last measured."""
            return 0

    spec = Wrapped.from_description(PIPELINE).describe()
    assert spec.commands["stop_soon"].description == (
        "Send an EOS event and wait for the EOS message it causes, while playing, for up "
        "to `timeout` seconds."
    )
    assert (
        spec.properties["level"].description == "The level of the thing, as it was last measured."
    )
    assert spec.commands["eos"].description.endswith("never carries it to the sinks.")
