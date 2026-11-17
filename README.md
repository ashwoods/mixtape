# mixtape: awesome mix vol. 1 🎵📼 

![Pypi version] ![Pypi license] ![Python versions] ![Build Status]

|[●▪▪●]| A python based `gstreamer` **player**-mini-framework.

**This project is in pre-alpha and the API is most likely to change**

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

* An asyncio `Player` to subclass, driven from the pipeline's bus.
* A declarative model of a player's commands and properties, and of its elements'.
* Auto-generated `cmdline` (`mixtape run`, `mixtape inspect`) and `http` (a control page and
  a REST and WebSocket API) interfaces, started from a pipeline description. `console` and
  `dbus` ones are still to come.
* Pluggable features: `leaks`, `tracers` and `perf` are built in, and `gst:NAME` makes any
  GStreamer tracer one.
* Pipeline metrics from GStreamer's own tracers, live, in a report, or for every test of a
  pytest suite, with budgets and baselines.

## Install

You can use `pip` to install `mixtape`:

    pip install mixtape

`mixtape` needs GStreamer and PyGObject (`gi`), which come from your system, not from PyPI:
`brew install gstreamer` on macOS, or
`apt install python3-gi python3-gst-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good`
on Debian/Ubuntu. Install `mixtape` into that Python, or into a venv created with
`--system-site-packages` from it.

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
# in a coroutine: see Usage below
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
every element. An exposed element's action signals are commands, `element.signal`, with the
parameters `arg1`, `arg2`... (GObject keeps no names of them), when the model supports their
types: booleans, numbers, strings, enums, caps and structures, and those results plus tag
lists, which come back as JSON (`mux.split-now`, `src.end-of-stream` of an `appsrc name=src`).
Elements inside playbin or decodebin are never user-named. When two elements
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
  properties, hover a link for its caps; bins without exposed elements start folded. The graph
  and the element sections follow the pipeline as it changes (a decodebin's elements, new pads,
  links and caps) without a reload, keeping open sections and values being typed.
* `POST /api/commands/<name>` with a JSON object of arguments (optional) runs a command and
  returns `{"result": ...}`.
* `GET`/`PATCH /api/elements/<element>` and `GET`/`PATCH /api/properties` read and set
  properties as `{name: value}`. A PATCH applies the values in order and stops at the first
  error.
* Errors are `{"detail": ..., "errors": [...]}`: 404 for an unknown name, 422 for a rejected
  value or a read-only property, 409 when a command cannot do what was asked, 500 for a pipeline
  error.
* `WS /api/events` sends the model, every readable property and the graph first (`{"type":
  "model", ...}`, `"values"`, `"graph"`), then the events as JSON (`state`, `property`, `eos`,
  `error`, `warning`, `position`, and `graph` and `model` when the pipeline changes, the
  `model` with the `values` of the elements it adds). It accepts
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
element's message when the pipeline fails, and `player.error` keeps the first one,
the cause, until the pipeline starts again.
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

`describe()` and `graph()` are snapshots. When the pipeline's shape changes while it runs (a
decodebin adds elements, pads come and go, links or caps change), the events say so: the
changes of `shape_interval` (0.1 s) come as one `GraphChanged` with the new `graph()`, and as
a `ModelChanged` with the new `describe()` when the exposed elements or their keys changed,
with the action signals of the elements added as commands, and none of those removed.

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
    features = ("leaks,interval=2",)  # names and classes, never instances


player = Player.from_description(description, features=["perf", "leaks,interval=5"])
```

    mixtape run "videotestsrc name=src ! queue name=q ! fakesink" --feature perf --http 8000
    mixtape inspect --feature leaks,interval=0.5 "videotestsrc ! fakesink"

The built-in ones:

* `leaks` publishes `leaks.alive`: the GStreamer objects created and not yet freed since it
  attached, by type, from GStreamer's `leaks` tracer; a count that keeps growing in a steady
  pipeline is a leak. `leaks.checkpoint` returns what changed since its last call and
  `leaks.reset` starts counting again. The tracer exists only if `GST_TRACERS` names it before
  GStreamer initialises, and there is one per process: enable it with `from_description` or
  `mixtape run`, which prepare the features first, and before anything else in the process
  initialises GStreamer, or set `GST_TRACERS=leaks` yourself.
* `tracers` runs GStreamer tracers with the player: `names` takes the syntax of `GST_TRACERS`
  (`;` between tracers, parameters in parentheses, commas included). They can be GStreamer's
  core tracers (`latency`, `rusage`, `stats`, …) or gst-plugins-rs ones (`queue-levels`,
  `buffer-lateness`, …). `log` adds `GST_TRACER:7` to `GST_DEBUG`, the only output of the
  core tracers, by default when `names` has one. What the environment already sets is kept,
  with a warning. `tracers.active` lists the tracers that run. A tracer that is not installed, or that
  GStreamer was initialised without, fails the player: like `leaks`, it needs to be first in
  the process. That rules out a pytest run whose earlier tests initialised GStreamer.

      mixtape run "videotestsrc ! queue ! fakesink" \
          --feature 'tracers,names=latency(flags=element+pipeline);rusage'

* `perf` measures the pipeline with GStreamer's own tracers and publishes `perf.summary`: n,
  mean, p50, p95, max and last of each metric by subject over the last `interval` seconds
  (default 1). The metrics, in `mixtape.perf.METRICS` with their units: `element.latency` and
  `pipeline.latency` (`src→sink`) from the core `latency` tracer; `pad.buffers`, `pad.bitrate`
  and `pad.interval` of each `element:pad` from the core `stats`; `pad.lateness`, `pad.push`
  and `queue.bytes`, `queue.buffers`, `queue.time` and `queue.fill` (0 to 1) from the
  gst-plugins-rs `buffer-lateness`, `pad-push-timings` and `queue-levels`, left out with a
  warning when they are not installed; `process.cpu`; and `thread.cpu` by thread name (on
  Linux and macOS; GStreamer names a streaming thread after its pad, `q:src`, and the
  unnamed ones are `other`; on Linux a name is cut to 15 bytes, so name an element shortly
  to keep its pad; idle ones, under 0.1% of a core, are left out). No Python runs on a streaming
  thread: the core tracers write to the debug log, which a thread reads from GStreamer's ring
  buffer logger, in memory (it replaces the default logger and echoes the lines that are not
  the tracers' to stderr; with a `GST_DEBUG_FILE` in the environment the default logger stays,
  writing there, and it echoes nothing; set `GST_DEBUG_FILE` when debugging a crash, as the
  lines of the last 0.25 s are lost when the process is killed), and the gst-plugins-rs ones
  write their logs when asked, once per window. With `out`, a folder gets
  `samples.jsonl` (every sample), `<metric>.csv` (`t_ns,subject,value`), `windows.jsonl` and,
  when the player is torn down, `summary.json`: n, mean, p50, p95, p99 and max of the whole
  run, the pipeline, its graph and the tracers; and `report.html`, all of it on one page that
  works offline: the graph with each element's latency, the queues' fill and each link's flow,
  a chart of each metric over the run, and the table of `summary.json`. On the `--http` page,
  `perf` shows the same live: on the graph, and in charts of the last five minutes. Times are
  nanoseconds since GStreamer initialised. Like `tracers`, it needs to be first in the process.

      mixtape run "videotestsrc num-buffers=300 ! queue ! fakesink" --feature perf,out=perf

* `gst:NAME` makes any GStreamer tracer a feature, a core one or one of a plugin on
  `GST_PLUGIN_PATH` in any language (C, Rust, Python), with no Python of its own. Its options
  are the tracer's parameters (`gst:queue-levels,file=x.csv` is `queue-levels(file=x.csv)` in
  `GST_TRACERS`). Its readable properties are read-only properties, `gst:NAME.property`,
  published when the tracer notifies them, and its action signals are commands,
  `gst:NAME.signal`, as for elements above: a structure it returns, such as a custom tracer's
  counters, comes back as JSON. Of the gst-plugins-rs tracers, `queue-levels`,
  `buffer-lateness` and `pad-push-timings` have one command, `write-log`, which writes their
  CSV to the file `arg1`; `pipeline-snapshot` has `snapshot`, which writes the pipelines'
  dot files. A tracer that `perf` reads is refused beside it (both would read the one tracer),
  and so is `leaks` (use the `leaks` feature: its tracer's signals crash the process when
  called out of order). Like `tracers`, it needs to be first in the process.

      mixtape run "videotestsrc ! queue name=q ! fakesink" --feature gst:queue-levels --http 8000
      curl -X POST localhost:8000/api/commands/gst:queue-levels.write-log -d '{"arg1": "q.csv"}'

### Perf in a test suite
Installing mixtape adds a pytest plugin that measures every test of any GStreamer suite with
the same tracers, whether it uses a `Player` or calls `Gst.parse_launch` itself:

    pytest --mixtape-perf perf-out --junitxml=junit.xml

Each test that ran a pipeline gets `perf-out/<test id>/` with `windows.jsonl`, `summary.json`
and `report.html` (its window is from the start of its setup to the end of its teardown; with
`--mixtape-perf-samples` also `samples.jsonl` and the CSVs, and `--mixtape-perf-interval
SECONDS` sets the window, default 1). `perf-out/index.json` has every test's outcome, duration,
folder, lost log lines and the worst p95, max and mean of each metric with the subject that had
it, and `perf-out/index.html` is the same as a page that links to the reports. The JUnit XML
gets `perf.<metric>.p95` and `perf.<metric>.max` properties on each test, such as
`perf.element.latency.p95` (nanoseconds). Without the option the plugin does nothing and
imports nothing of GStreamer.

Two checks catch a test that got slower. A budget, `@pytest.mark.perf`, fails the test at the
end of its call when the worst subject of a metric is over its limit over the whole run so far:

```python
@pytest.mark.perf(pipeline_latency_p95="20ms", process_cpu_mean="150%", queue_fill_max=0.9)
def test_stream(): ...
```

The keywords are a metric (`.` as `_`) and a statistic (`n`, `mean`, `p50`, `p95`, `p99` or
`max`, as in `summary.json`); the limit is a string with a unit (`ns`, `us` or `µs`, `ms`, `s` for
the metrics in nanoseconds; `%` for CPU, and for a ratio like `queue.fill`, where `90%` is
0.9) or a number in the metric's own unit. A failure says `perf budget: pipeline.latency p95 is
31.2 ms on videotestsrc0→fakesink, over 20 ms`; a metric the test has no samples of fails too. An
unknown metric, statistic or unit is a usage error naming the keyword, before any test runs. The
budgets go to the JUnit properties as `perf.budget.<metric>.<stat>`. Without `--mixtape-perf`
the marker is registered and does nothing. A budget needs the samples of the whole run, which
the recorder keeps in memory (8 bytes each) for the summary anyway.

The other check is a stored baseline: `--mixtape-perf-baseline BASE`, the `perf-out` of an
earlier session, compares this session with it at the end as `mixtape perf compare` does
(below), writes `perf-out/compare.json`, prints the result in its own section and fails the
session (exit status 1) when there are regressions.

Tracers only exist when `Gst.init` creates them, so the plugin initialises GStreamer before the
first conftest is read, and stops with a usage error when something did so earlier. It gives
`GST_TRACERS` back to the environment, so child processes start no tracers. It is not aware of
`pytest-xdist`: each worker would write its own index.

### Comparing perf runs
`mixtape perf compare BASE NEW` finds what got slower between two sessions of
`pytest --mixtape-perf` (folders with an `index.json`) or two single runs (folders with a
`summary.json`, as `mixtape run --feature perf,out=DIR` writes). It compares the p95 and max of
the metrics where higher is worse and that are steady enough (latencies, CPU, queue levels;
not buffers/s, bitrate or the interval, nor lateness and push time, which swing by tens of ms
between two runs of a short test: bound those with a budget): for each test the worst of its
subjects in a session, for each subject in a run. A value is a regression when it grew by more
than `--threshold` percent (default 50) of the base *and* by more than a floor of its unit
(100 µs, 5 points of CPU, 5% of a queue, one buffer, 4 KiB; `FLOORS` in
`mixtape/perf/compare.py`). It prints the regressions, the improvements beyond the same bounds,
what only one side has (added or removed, never a regression) and a line of counts, and exits 1
on a regression, 2 when a folder is not one of those. `--json` prints the comparison as JSON
instead.

    mixtape perf compare perf-main perf-pr --threshold 30

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
[GObject]: https://docs.gtk.org/gobject/
[GStreamer tutorials]: https://gstreamer.freedesktop.org/documentation/tutorials/index.html
[Pypi version]: https://img.shields.io/pypi/v/mixtape.svg
[Pypi license]: https://img.shields.io/pypi/l/mixtape.svg
[Python versions]: https://img.shields.io/pypi/pyversions/mixtape.svg
[Build Status]: https://github.com/ashwoods/mixtape/actions/workflows/ci.yml/badge.svg?branch=main

