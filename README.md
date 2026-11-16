# mixtape: awesome mix vol. 1 🎵📼 

![Pypi page] ![Pypi license] ![Pypi version] ![Build Status] ![Quality Gate Status]

|[●▪▪●]| A python based `gstreamer` **player**-mini-framework.

**This project is in pre-alpha and most functionality is incomplete and the API is
most likely to change**

[Gstreamer] is a pipeline based framework for building complex multimedia processing 
systems. It is written in C, based on [GObject], and offers several bindings in other languages,
including python.

If you are unfamiliar with Gstreamer, you should start with the [GStreamer tutorials]. 

`mixtape` offers a few utility classes and functions for a gstreamer `application`,
and auto generates service interfaces for your application.

## Project goal

Python is a great language for prototyping and integration tests. The goal of the project is 
to have something in between `gst-launch` and writing a full python application for testing
and prototyping.


*Features*:

* Pluggable declarative definitions of Player features.
* Optional loops (glib, asyncio, ...) (WIP)
* Auto-generated `cmdline`, `console`, `http` and `dbus` service interfaces* (WIP)
* `cmdline`, `console`, `http` and `dbus` service cmdline commands to jump start services 
  from pipeline descriptions (WIP)

## Quickstart

## Install

You can use `pip` to install `mixtape`:

    pip install mixtape

`mixtape` needs GStreamer and PyGObject (`gi`), which come from your system, not from PyPI:
`brew install gstreamer` on macOS, or
`apt install python3-gi python3-gst-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good`
on Debian/Ubuntu. Install `mixtape` into that Python, or into a venv created with
`--system-site-packages` from it.

## Development

You need [uv] and GStreamer with its Python bindings:

    brew install uv gstreamer   # macOS (Homebrew)
    sudo apt install python3-gi python3-gst-1.0 \
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good   # Debian/Ubuntu

Then run every check (ruff lint, ruff format, mypy, pytest) with:

    scripts/check

The first run creates `.venv` from the Python that has `gi` (Homebrew's `python3`, or
`/usr/bin/python3`; set `MIXTAPE_PYTHON` to choose another) with `--system-site-packages`,
so the venv sees PyGObject and GStreamer's Python overrides, then `uv sync`s the dev
dependencies. Arguments go to pytest: `scripts/check -k eos`. After that, `uv run pytest`,
`uv run ruff format` and `uv run mypy` work on their own, unless `UV_PYTHON` or a global
`uv python pin` asks for another Python (then uv replaces the venv; `scripts/check` guards
against that). To switch to another Python, `rm -rf .venv` and run `scripts/check` again.

Without a native GStreamer, run the same checks in Docker (Ubuntu 24.04, as CI does):

    docker compose run --rm test

`tests/test_gst_c_crash.py` runs `mixtape run` against the failing elements of
[gst-c-crash](https://github.com/ashwoods/gst-c-crash) and skips unless they are found: build it
and run `GST_PLUGIN_PATH=<build>/gst scripts/check`.

## Command line
`mixtape run` plays a `gst-launch` style pipeline until EOS or an error, like
`gst-launch-1.0 -e`; the description can be one quoted string or several words:

    mixtape run "videotestsrc num-buffers=150 ! autovideosink"
    python -m mixtape run -v videotestsrc ! fakesink

It exits 0 on EOS, 1 on a pipeline error or a pipeline that cannot stop, 2 on a bad description
or usage. The first Ctrl-C (or SIGTERM) sends EOS and stops; a second one stops at once and exits
130, and a third kills it. A pipeline whose streaming thread hangs gets 5 s for the EOS and 5 s
to stop, then `mixtape run` exits 1 naming the elements still changing state. `-v` logs at info
level and shows the error's debug string, `-vv` logs at debug level. On macOS, `mixtape run`
runs the main run loop so video windows appear.

### Inspecting and setting properties
A `Player` describes what it exposes: its properties, with types, ranges and current values,
and its commands. Declare your own with `prop()`, an alias of an element property, `@prop` on a
method, a read-only property computed by it, and `@command`; the class `description` is the pipeline `mixtape` builds for it:

```python
# demo.py
from mixtape import Player, command, prop


class Demo(Player):
    description = "videotestsrc name=src num-buffers=100 ! x264enc name=enc ! fakesink"

    bitrate = prop("enc.bitrate", description="Target bitrate in kbit/s")

    @command
    def shout(self, word: str, times: int = 2) -> str:
        """Repeat a word."""
        return word * times
```

```python
player = Demo.from_description(Demo.description)
spec = player.describe()  # a pydantic model: spec.model_dump_json() is JSON with JSON Schema types
player.get("bitrate")  # 2048, the same as player.get("enc.bitrate")
await player.set("bitrate", "4000")  # "4000" is validated and converted to 4000
await player.invoke("shout", word="hi")  # "hihi"
```

`mixtape inspect` prints the same model without playing, from a description or a
`module:Class` (found in the current directory too), as text or with `--json` as JSON.
`mixtape run --set PATH=VALUE` sets properties before playing, and can be repeated:

    mixtape inspect demo:Demo
    mixtape inspect --json "videotestsrc name=src ! fakesink"
    mixtape run demo:Demo --set bitrate=4000 --set src.pattern=ball

Only the named elements (`name=src`) are exposed, `--expose-all` (or `expose_all=True`) exposes
every element. Elements inside playbin or decodebin are never user-named. When two elements
share a name, each is keyed by its dotted path (`a.q`, `b.q`) and a bare `q` is ambiguous.
A value is validated against the property's type and range, and one that is out of
range or unknown is rejected: `run` prints `error: ...` and exits 2. The "mutable: playing"
hint in `inspect` only reports what the element declares; it is not enforced, and setting a
property while playing is up to the element.

## HTTP interface
The optional `http` extra (FastAPI and uvicorn, no threads: the server runs in the player's
own asyncio loop) controls a player from a browser:

    pip install 'mixtape[http]'
    mixtape run --http 8080 "videotestsrc ! autovideosink"    # http://127.0.0.1:8080

`--http [HOST:]PORT` plays the pipeline and serves it until Ctrl-C: an EOS or a pipeline error
does not end it (errors are printed), so the page can play again. The first Ctrl-C sends EOS,
stops and exits 0. In code: `async with mixtape.interfaces.http.serve(player, host, port)`.

* `GET /` the control page, `GET /api/model` the player's `describe()`, `GET /api/graph` its
  `graph()` (elements and the links between them, with their caps), `/docs` the OpenAPI.
  The page is generated from the model: transport and position, a form per command, a control
  per property, updated live. Press `/` to filter properties; "Modified" shows those that
  differ from their default. On top, the pipeline graph: click an element to jump to its
  properties, hover a link for its caps; bins without exposed elements start folded.
* `POST /api/commands/<name>` with a JSON object of arguments (optional) runs a command and
  returns `{"result": ...}`.
* `GET`/`PATCH /api/elements/<element>` and `GET`/`PATCH /api/properties` read and set
  properties as `{name: value}`. A PATCH applies the values in order and stops at the first
  error.
* Errors are `{"detail": ..., "errors": [...]}`: 404 for an unknown name, 422 for a rejected
  value or a read-only property, 409 when a command cannot do what was asked, 500 for a pipeline
  error.
* `WS /api/events` sends every readable property first (`{"type": "values", ...}`), then the
  events as JSON (`state`, `property`, `eos`, `error`, `warning`, `position`). It accepts
  `{"id": 1, "command": "play", "args": {}}` and `{"id": 2, "set": {"src.pattern": "ball"}}`
  and answers each with `{"type": "reply", "id": ..., "result": ...}` or `"error": {"status",
  "detail"}`.

There is no authentication: it binds to 127.0.0.1 by default, and `--http 0.0.0.0:8080` warns
that anyone who can reach the port can control the player. A request whose `Origin` is not the
`Host` it was sent to is refused (403), and so is one whose `Host` is a DNS name other than
`localhost` or this machine's own name (DNS rebinding); bound to a loopback address, the `Host`
must be a loopback one. Requests without an `Origin`, like curl's, pass.

## Usage
`Player` wraps a `Gst.Pipeline` and drives it from an asyncio loop. Create one from a
`gst-launch` style description with `Player.from_description`; it needs no running loop.
The first `await` on the player attaches it to the running loop, and `async with` tears it
down (sets the pipeline to NULL) when the block ends:

```python
import asyncio

from mixtape import Player


async def main() -> None:
    async with Player.from_description("videotestsrc ! fakesink") as player:
        await player.play()
        await asyncio.sleep(5)
        await player.stop()


asyncio.run(main())
```

`Player.run` is the shortcut for scripts: it plays a description (or a `Gst.Pipeline`) until
EOS and tears the player down. A pipeline error raises `PlayerPipelineError`:

```python
from mixtape import Player

Player.run("videotestsrc num-buffers=100 ! fakesink", timeout=30)
```

Errors reach the code that awaits: `await player.play()` raises `PlayerPipelineError` with the
element's message when the pipeline fails, and `player.error` keeps the last one.
`await player.play_until_eos()` returns on the next EOS. Calling it again plays the stream
again, from READY when the pipeline is still at the earlier EOS.

Every bus message is also an event. Subscribe with `events()` to build services on top
of the player, and use `add_handler` to react to one kind of message:

```python
import asyncio

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player
from mixtape.events import Eos, StateChanged


async def main() -> None:
    async with Player.from_description("videotestsrc num-buffers=100 ! fakesink") as player:
        events = player.events()
        remove = player.add_handler(Gst.MessageType.TAG, lambda message: print(message))
        await player.play()
        async for event in events:
            if isinstance(event, StateChanged):
                print(event.old, "->", event.new)
            elif isinstance(event, Eos):
                break
        remove()


asyncio.run(main())
```

Subclass `Player` to build an application. Override the state methods, take extra arguments,
and call into the loop from GStreamer's threads with `call_soon_threadsafe` and
`run_coroutine_threadsafe`:

```python
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player


class Camera(Player):
    def __init__(self, pipeline: Gst.Pipeline, name: str) -> None:
        super().__init__(pipeline)
        self.name = name
        self.add_handler(Gst.MessageType.APPLICATION, self.on_application)

    async def play(self) -> Gst.StateChangeReturn:
        print(f"{self.name} is starting")
        return await super().play()

    def on_application(self, message: Gst.Message) -> None:
        print(message.get_structure().get_name())
```

Constructing a dynamic pipeline and passing it to the player:

```python
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player

Gst.init(None)
pipe = Gst.Pipeline.new("dynamic")
src = Gst.ElementFactory.make("videotestsrc")
sink = Gst.ElementFactory.make("fakesink")
pipe.add(src)
pipe.add(sink)
src.link(sink)

player = Player(pipe)
```

## Features
A feature is a plugin of a player: it adds commands and live properties to the model, next to
the elements', as `feature.name`. They show up in `describe()`, `mixtape inspect`, the HTTP API
and the control page like any other. Enable one three ways: as a class attribute of a player
class, with the `features` argument (`"name,key=value"` strings, classes or instances), or
with `--feature` on the command line, which can be repeated:

```python
class Camera(Player):
    features = ("stats,interval=2",)  # names and classes, never instances


player = Player.from_description(description, features=["stats", "leaks,interval=5"])
```

    mixtape run "videotestsrc name=src ! queue name=q ! fakesink" --feature stats --http 8000
    mixtape inspect --feature leaks,interval=0.5 "videotestsrc ! fakesink"

The built-in ones:

* `stats` publishes `stats.rates`: the buffers and kilobits per second leaving each exposed
  element, over the last `interval` seconds (default 1). It counts with a pad probe on the source
  pads an element has when the player is set up, so a sink has no rate.
* `leaks` publishes `leaks.alive`: the GStreamer objects created and not yet freed since it
  attached, by type, from GStreamer's `leaks` tracer; a count that keeps growing in a steady
  pipeline is a leak. `leaks.checkpoint` returns what changed since its last call and
  `leaks.reset` starts counting again. The tracer exists only if `GST_TRACERS` names it before
  GStreamer initialises, and there is one per process: enable it with `from_description` or
  `mixtape run`, which prepare the features first, and before anything else in the process
  initialises GStreamer, or set `GST_TRACERS=leaks` yourself.

### Writing a feature
Subclass `Feature` with a `name`, the options as `__init__` keyword parameters (validated, so
`--feature heartbeat,interval=2` gives a float), and `@command` methods and `@prop` getters as a
`Player` can. `attach` runs on the player's loop when it is set up and `detach` when it is torn
down; `notify("beats")` publishes the current value of a property to the event streams, on the
loop thread only. `prepare` runs once, as the player is built: before GStreamer is initialised
only when it is built from a description (environment variables it sets stay set); `check` runs when the player is built and may raise `FeatureError` if the feature
cannot work with it. Getters and commands must also work when the player is not set up, as
`mixtape inspect` reads every property.

```python
# heartbeat.py
import asyncio
import os

from mixtape import Feature, Player, command, prop


class Heartbeat(Feature):
    name = "heartbeat"
    requires = ("identity",)  # GStreamer elements or tracers it needs

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._beats = 0
        self._task: asyncio.Task[None] | None = None

    def prepare(self) -> None:  # before GStreamer initialises: environment variables
        os.environ.setdefault("GST_DEBUG_NO_COLOR", "1")

    def attach(self, player: Player) -> None:
        self._beats = 0
        self._task = asyncio.get_running_loop().create_task(self._beat())

    def detach(self, player: Player) -> None:
        if self._task:
            self._task.cancel()

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self._beats += 1
            self.notify("beats")

    @prop
    def beats(self) -> int:
        """Beats since the player was set up"""
        return self._beats

    @command
    def reset(self) -> None:
        """Start counting again"""
        self._beats = 0
        self.notify("beats")
```

Pass it as `features=[Heartbeat()]`, or register it by name in your package's entry points to
use it with `--feature heartbeat` and in `features=["heartbeat"]`; `mixtape inspect --help`
lists the ones installed:

```toml
[project.entry-points."mixtape.features"]
heartbeat = "heartbeat:Heartbeat"
```

----

    ....___.___.___.___.___.__....
    |:                          :|
    |:    Awesome mix vol 1     :|
    |:     ,-.   _____   ,-.    :|
    |:    ( `)) [_____] ( `))   :|
    |:     `-`   ' ' '   `-`    :|
    |:     ,______________.     :|
    |...../::::o::::::o::::\.....|
    |..../:::O::::::::::O:::\....|
    |---/----.----.----.-----`---|

We are Groot.

[GStreamer]: https://gstreamer.freedesktop.org/
[uv]: https://docs.astral.sh/uv/
[GObject]: https://developer.gnome.org/gobject/stable/
[GStreamer tutorials]: https://gstreamer.freedesktop.org/documentation/tutorials/index.html
[Pypi page]: https://img.shields.io/pypi/v/mixtape.svg
[Pypi license]: https://img.shields.io/pypi/l/mixtape.svg
[Pypi version]: https://img.shields.io/pypi/pyversions/mixtape.svg
[Build Status]: https://github.com/ashwoods/mixtape/actions/workflows/ci.yml/badge.svg?branch=main
[Quality Gate Status]: https://sonarcloud.io/api/project_badges/measure?project=ashwoods_mixtape&metric=alert_status
[Requirements Status]: https://requires.io/github/ashwoods/mixtape/requirements.svg?branch=main

