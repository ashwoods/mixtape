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

## Command line
`mixtape run` plays a `gst-launch` style pipeline until EOS or an error, like
`gst-launch-1.0 -e`; the description can be one quoted string or several words:

    mixtape run "videotestsrc num-buffers=150 ! autovideosink"
    python -m mixtape run -v videotestsrc ! fakesink

It exits 0 on EOS, 1 on a pipeline error, 2 on a bad description or usage. The first Ctrl-C
(or SIGTERM) sends EOS and stops; a second one stops at once and exits 130, and a third kills it if
GStreamer blocks while stopping. `-v` logs at info
level and shows the error's debug string, `-vv` logs at debug level. On macOS, `mixtape run`
runs the main run loop so video windows appear.

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
`await player.play_until_eos()` returns on the next EOS, and works again after `stop()`.

Every bus message is also an event. Subscribe with `events()` to build services on top
of the player, and use `add_handler` to react to one kind of message:

```python
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player
from mixtape.events import Eos, StateChanged

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

## Player base interface (Draft)

(WIP)

    - Player.from_desc

    - player.run

    - player.play

    - player.stop

    - player.pause


## Feature interface (Draft)

(WIP)

    from mixtape.features import AwesomeFeature, Attachable


    class WebRTC(AwesomeFeature, Attachable):

        name = 'webrtc'
        plugins_dependencies = ['webrtcbin', 'nice']
        bin_desc = ""

    def check(self, pipeline):
        pass

    def attach(self, pipeline, element, pad):
        pass

    def detach(self, pipeline, element, pad):
        pass

    def check(self, pipeline):
        pass

    def init(self, pipeline)
        pass

    def exit(self, pipeline)
        pass

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
[Build Status]: https://github.com/ashwoods/mixtape/actions/workflows/ci.yml/badge.svg?branch=master
[Quality Gate Status]: https://sonarcloud.io/api/project_badges/measure?project=ashwoods_mixtape&metric=alert_status
[Requirements Status]: https://requires.io/github/ashwoods/mixtape/requirements.svg?branch=master

