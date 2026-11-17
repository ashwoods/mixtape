# Writing an application

mixtape is a library first: `mixtape run` is a small program built on it. This page builds
applications on the same pieces: a `Player` that drives a pipeline from an asyncio loop,
the events it publishes, a model of its properties and commands that the [command line](cli.md)
and the [HTTP API](http.md) read, and features that add to that model.

It starts with the smallest programs and ends with one application of its own, a test card:
a `Player` subclass with a pattern to choose, a frame counter and a command.

The names are in `mixtape` (`Player`, `Feature`, `prop`, `command`), `mixtape.events` and
`mixtape.exceptions`.

## Play a pipeline

`Player.from_description` builds a player from a `gst-launch-1.0` description. It needs no
running loop: the first `await` on the player attaches it to the loop that is running, and
`async with` tears it down, setting the pipeline to NULL, when the block ends.

```python
--8<-- "examples/lib_lifecycle.py:lifecycle"
```

```sh
python3 examples/lib_lifecycle.py
```

```
playing
paused
null
```

`play()`, `pause()`, `ready()` and `stop()` return when the pipeline has reached the state, not
before. `stop()` sets it to NULL, after an EOS with `stop(eos=True)`. A player that is torn down can
be used again. Without `async with`, call `player.setup()` on the loop to attach it and
`player.teardown()` to detach it.

### Until the end of the stream

`await player.play_until_eos()` plays and returns on the next EOS. The pipeline is still playing
then; calling it again plays the stream again, from READY:

```python
--8<-- "examples/lib_until_eos.py:until_eos"
```

```sh
python3 examples/lib_until_eos.py
```

```
run 1: playing at the end of the stream
run 2: playing at the end of the stream
```

`Player.run` is the shortcut for a script: it plays a description (or a `Gst.Pipeline`) until
EOS, tears the player down and returns. It starts the loop itself, so it cannot be called from
a coroutine. It raises `TimeoutError` when `timeout` seconds pass first, and
`PlayerPipelineError` when the pipeline fails (see below).

```python
--8<-- "examples/lib_run.py:run"
```

```sh
python3 examples/lib_run.py
```

```
done
```

### From a pipeline you built

A pipeline made in code, with `Gst.ElementFactory` and `link`, goes to `Player(pipeline)`.
GStreamer has to be initialised before the elements are made, so call `Gst.init(None)` first.
`from_description` does it for you, and wraps a description that is a single element in a
pipeline.

```python
--8<-- "examples/lib_dynamic.py:build"
```

```sh
python3 examples/lib_dynamic.py
```

```
elements: dynamic, src, sink
links: [('src', 'sink')]
played
```

Only elements with a name of your own are in the model: `describe().elements` lists the
pipeline, `src` and `sink` here, and not `videotestsrc0`. Give a name to what you want to
read and set, or pass `expose_all=True` to expose every element.

## When it goes wrong

Everything that can fail has an exception, all of them subclasses of `mixtape.exceptions.MixTapeError`:

| Exception               | Raised when                                                                  |
|-------------------------|------------------------------------------------------------------------------|
| `PlayerDescriptionError`| `from_description` cannot parse the description                              |
| `PlayerPipelineError`   | the pipeline posts an error: `source`, `message`, `debug` and the `gerror`   |
| `PlayerSetStateError`   | a state change fails, or the pipeline is stuck changing state                |
| `PlayerNotConfigured`   | a method needs the running loop of the player and there is none              |

A typo in the description fails at once, with the parser's message:

```python
--8<-- "examples/lib_errors.py:description"
```

An element error reaches the code that awaits. `await player.play()` raises
`PlayerPipelineError` with the element's own message when the pipeline fails to start, and the
player has already set the pipeline to NULL:

```python
--8<-- "examples/lib_errors.py:start"
```

A pipeline that fails later fails the `await player.play_until_eos()` that waits for it. The
first error is the cause, because an element downstream often follows it with errors of its own.
`player.error` keeps it until the pipeline starts again:

```python
--8<-- "examples/lib_errors.py:playing"
```

The program calls the three functions in turn:

```sh
python3 examples/lib_errors.py
```

```
Invalid pipeline 'videotestsrc ! nosuchelement ! fakesink': no element "nosuchelement"
filesink0: Could not open file "/nonexistent/x" for writing.
state: null
flaky: Failed after iterations as requested.
player.error is the exception: True
error after playing again: None
```

The player also logs an error and a warning through the `mixtape.player` logger. The program
turns that off to keep its output to what it prints.

An error that happens while nothing awaits, a pipeline that plays while the program sleeps, does
not raise anywhere: it is an `Error` event and `player.error`.

## Events

Every message on the pipeline's bus is also an event. `player.events()` subscribes to the events
published from now on, so call it before the change that you want to see. It returns an async
iterator that ends when the player is torn down, or when you call its `close()`.

| Event            | Fields                                  | Published when                                         |
|------------------|-----------------------------------------|--------------------------------------------------------|
| `StateChanged`   | `old`, `new`, `pending`                 | the pipeline itself changed state                      |
| `Eos`            |                                         | the stream ended                                       |
| `Error`          | `error`                                 | the pipeline failed; the player set it to NULL already |
| `Warning`        | `source`, `message`, `debug`            | an element posted a warning                            |
| `PropertyChanged`| `path`, `element`, `property`, `value`  | an exposed element's property changed, on any thread, or a computed property was notified |
| `Position`       | `position`, `duration`                  | either changed, in seconds; only for `events(position=True)` |
| `GraphChanged`   | `graph`                                 | the pipeline's elements, pads, links or caps changed   |
| `ModelChanged`   | `model`                                 | the exposed elements or their keys changed             |
| `Message`        | `message`                               | any bus message, after the specific event for it       |

They are frozen dataclasses, so they work with `match`. This program reads a short stream,
with its position, and sets a property while it waits:

```python
--8<-- "examples/lib_events.py:events"
```

```sh
python3 examples/lib_events.py
```

```
src.pattern = ball
playing
the position moves
end of stream
```

`Position` is read every `Player.position_interval` seconds (0.25) while some stream asks for it,
and published when the value changed. A stream that asks for them gets the current one at once.

### Handling one kind of message

`add_handler` calls a function on the loop for the bus messages of a type, after the
built-in handling of them. It returns the function that removes it. A handler that raises is
logged and does not stop the others.

```python
--8<-- "examples/lib_handler.py:handler"
```

```sh
python3 examples/lib_handler.py
```

```
a stream started
done
```

Use `add_handler` to react to one kind of message with a function, and `events()` to read
everything in order, in a loop of your own.

### When the pipeline changes shape

`describe()` and `graph()` are snapshots. A pipeline changes while it runs: a decodebin adds
elements, pads come and go, links are made and caps are negotiated. The player watches for it
and publishes what changed as `GraphChanged` with the new `graph()`. The changes of
`Player.shape_interval` (0.1 s) come together as one event, and only when the graph differs from
the last one published.

When the elements the model exposes change, or their keys do (a second `q` in another bin makes
the first one `a.q`), `ModelChanged` follows with the new `describe()`. It has the action signals
of the elements that came as commands, and none of those that went. A decodebin's elements are
never named by you, so what it adds is a `GraphChanged` and no `ModelChanged`, unless the player
exposes all elements.

This program adds a named branch to a tee while the pipeline plays:

```python
--8<-- "examples/lib_shape.py:shape"
```

```sh
python3 examples/lib_shape.py
```

```
exposed: t, main
links: 5
exposed: t, main, extra
```

The web page of the [HTTP API](http.md) follows the same events: its graph and its element
sections change without a reload.

## Subclass Player

Subclass `Player` for an application of your own: its pipeline, its properties, its commands. The
test card has a `videotestsrc` and a `fakesink`, a `pattern` to choose, a count of the frames
that reached the sink and a command to show a pattern.

```python
--8<-- "examples/testcard.py:player"
```

What each part does:

- `description` is the pipeline of the class. `Testcard.from_description(Testcard.description)`
  builds it, and so do `mixtape run testcard:Testcard` and `mixtape inspect testcard:Testcard`. A
  subclass with its own `__init__` takes the pipeline and passes the keyword arguments on
  (`expose_all`, `features`) to `super().__init__`.
- `prop("src.pattern")` declares an alias of an element property: `pattern` in the model, with the
  type, the range and the value of `src.pattern`, and a description of your own (and a `label`
  for the page). Reading `card.pattern` is `card.get("pattern")`. A property is set with
  `await card.set("pattern", "ball")`, which validates the value and changes nothing when it is
  rejected. A typo in the target fails when the player is built.
- `@prop` on a method declares a read-only property that the method computes. Its return type
  hint is the type in the model, and a missing one fails when the class is defined. The first
  line of its docstring is the description. `self.notify("frames")` publishes its current value
  as a `PropertyChanged`.
- `@command` on a method, sync or async, declares a command. The type hints of its parameters
  are the types of its arguments, and a default makes an argument optional. `Literal` is an
  enumeration. The arguments are validated before the method runs, and the return hint is the
  type of the result. It takes no `*args` or `**kwargs`. The built-in `play`, `pause`, `stop`,
  `eos` and `seek` are commands of every player, and a command calls your override of a method
  such as `play`.
- A pad probe, a signal handler or a callback of an element runs on a GStreamer thread, where
  the loop's objects are off limits. `player.call_soon_threadsafe(callback, *args)` runs a
  function on the loop, and `player.run_coroutine_threadsafe(coroutine)` runs a coroutine there
  and returns a `concurrent.futures.Future` with its result. The probe uses the first: it
  counts each frame on the loop, which also publishes the count, as `notify` is for the loop
  thread only.

`describe()` returns the player's model as a pydantic model (`describe().model_dump_json()` is
JSON with JSON Schema for every type), `get(path)` reads a property, `set(path, value)` sets one
and `invoke(name, **arguments)` runs a command: the calls that the command line and the HTTP API
make. A name that does not exist is an `UnknownNameError`, a rejected value or argument an
`InvalidValueError`, and a property that cannot be written a `NotWritableError`.

```python
--8<-- "examples/testcard.py:main"
```

```sh
python3 examples/testcard.py
```

```
commands: eos, pause, play, seek, show, stop
properties: duration, frames, pattern, position, state
pattern: smpte
show: snow
from a thread: smpte
rejected: Invalid arguments for 'show': pattern: Input should be 'smpte', 'ball' or 'snow'
frames: 10
```

The command line reads the same model without playing, from the module and the class. Here is
the command of the test card:

```sh
PYTHONPATH=examples mixtape inspect testcard:Testcard | grep 'show('
```

```
  show(pattern: smpte|ball|snow = smpte)                  Show a test pattern.
```

See [Command line](cli.md) for what else `mixtape run` and `mixtape inspect` do with a class,
and [HTTP API](http.md) for the control page it gets.

## Write a feature

A feature is a plugin of a player: it adds commands and live properties to the model, next to
the elements', as `feature.name`. They show up in `describe()`, `mixtape inspect`, the HTTP API
and the control page like any other. [Measuring performance](perf.md) uses the built-in
`perf`; this one counts beats.

A feature is a `Feature` subclass with a `name`, its options as the keyword parameters of
`__init__`, and `@command` methods and `@prop` getters like a player's. Its hooks run in this
order:

- `prepare` runs once, before GStreamer is initialised, when the player is built from a
  description: the place to set environment variables.
- `initialised` runs once GStreamer is initialised and before the pipeline is built from a
  description: what has to be in place before an element is made.
- `check` runs when the player is built, once `requires` (GStreamer elements or tracers) is
  met. It raises `FeatureError` if the feature cannot work with this pipeline.
- `attach` runs when the player is set up, on its loop: start what runs beside the pipeline.
- `detach` runs when the player is torn down, with the pipeline in NULL: stop it.

```python
--8<-- "examples/heartbeat_plugin/heartbeat.py:feature"
```

- The options are validated. `heartbeat,interval=0.5` on the command line gives a float, and a
  value that does not fit is a `FeatureError` before anything starts.
- `notify("beats")` publishes the current value of the property `beats` as a `PropertyChanged` of
  `heartbeat.beats`, and does nothing while the feature is detached. Like the player's, it is for the
  loop thread only.
- Getters and commands must also work when the player is not set up, because `mixtape inspect`
  reads every property: answer from what you have. `self.player` is the player while attached.
- A feature declares computed properties only. Its name cannot be that of an element of the
  pipeline.

Enable it with the `features` argument of a player, as an instance, a class or a
`"name,key=value"` string. A player class can enable features for every instance with its own
`features` attribute, such as `features = ("heartbeat,interval=2",)`: names and classes, not
instances.

```python
--8<-- "examples/heartbeat_plugin/heartbeat_demo.py:demo"
```

```sh
python3 examples/heartbeat_plugin/heartbeat_demo.py
```

```
in the model: heartbeat.reset heartbeat.beats
heartbeat.beats = 3
reset: None 0
detached: True
Invalid options for feature 'heartbeat': interval: Input should be greater than 0
heartbeat needs a pipeline with a sink
```

### Make it available by name

Register the class in the `mixtape.features` entry points of your package, and `--feature
heartbeat`, `features=["heartbeat"]` and `mixtape inspect --help` know it. This is the whole
`pyproject.toml` of the package, next to its `heartbeat.py`:

```toml
--8<-- "examples/heartbeat_plugin/pyproject.toml"
```

Install it into the environment that has mixtape:

```sh
pip install ./examples/heartbeat_plugin
```

```sh
mixtape inspect --feature heartbeat,interval=0.5 "videotestsrc ! fakesink" | grep heartbeat
```

```
Features: heartbeat
  heartbeat.reset()                                       Start counting again
  heartbeat.beats  r-  integer                    = 0       Beats since the player was set up
```

The same package gives the feature to `mixtape run`, with `--http` for the control page: `beats`
is a property that updates live, and `reset` has a form.
