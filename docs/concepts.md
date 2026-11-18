# Concepts

This page explains how mixtape's pieces fit together: what a player owns, what its model holds,
which thread runs what, what a feature is, why some features have to come first in the process,
and what is generated from the model. It has nothing to run. [Writing an application](library.md)
builds a player step by step, and the [command line](cli.md) and the [HTTP API](http.md) use one.

## A player owns one pipeline { #a-player-owns-one-pipeline }

A [`Player`](reference.md#mixtape.Player) wraps one `Gst.Pipeline`, made from a
`gst-launch-1.0` description (`Player.from_description`) or in code (`Player(pipeline)`). It
attaches to the asyncio loop that is running when you first `await` one of its methods (that
is `setup`), reads
the pipeline's bus there, and publishes each bus message as an
[event](library.md#events). Everything else on this page is made from the player: its model, its
features and its interfaces.

One player has one pipeline, one model and one stream of events. Two players in one process do
not see each other, with one exception, the [tracers](#tracers-once-per-process). mixtape has
nothing that groups players ([Two pipelines](two-pipelines.md#what-mixtape-does-across-two-players)).

## The model { #the-model }

The model is what a player lets you read, set and call: its properties and its commands. It is
data, with a JSON Schema for every type, and
[`player.describe()`](reference.md#mixtape.Player.describe) returns it. Every interface below reads it, and
[`get(path)`](reference.md#mixtape.Player.get), [`set(path, value)`](reference.md#mixtape.Player.set)
and [`invoke(name, **arguments)`](reference.md#mixtape.Player.invoke) are the calls that they make: a value is validated against its type before anything changes.

It has these parts, each under its own name:

| In the model | Comes from | Example |
|--------------|------------|---------|
| Built-in properties | every player has them | `state`, `position`, `duration` |
| Built-in commands | every player has them | `play`, `pause`, `stop`, `eos`, `seek` |
| Element properties | the GObject properties of an exposed element | `vol.volume` |
| Aliases | `prop("vol.volume")` on a `Player` subclass: an element property under a name of your own | `gain` |
| Computed properties | a method with `@prop`, typed by its return hint | `frames` |
| Commands | a method with `@command`, typed by its parameters | `shout(word, times)` |
| Element commands | the action signals of an exposed element: `key.signal`, with the arguments `arg1`, `arg2`... | `src.end-of-stream` |
| Feature properties and commands | a [feature](#features): `feature.name` | `leaks.checkpoint` |

[Writing an application](library.md#subclass-player) declares each of the middle four, and
[Action signals are commands](cli.md#action-signals-are-commands) shows an element's.

A command can say when it can run. `@command(when=check)` takes a function of the player that
returns `True`, or the reason the command cannot run now. The player keeps the map of the ones
that cannot, `{name: reason}`, in `player.unavailable()` and in `describe()`, refuses such a
command in `invoke` (a 409 over HTTP), publishes a `CommandsChanged` event when the map
changes, and the control page disables the button with the reason. `eos`, for one, needs a
playing pipeline: see [Commands that cannot run](library.md#commands-that-cannot-run).

### Only named elements are exposed { #only-named-elements-are-exposed }

The model has the elements that you name, `name=vol` in a description. `audiotestsrc0` is a
name that GStreamer made up, and it is not exposed: it is not a name to build on, and a
pipeline can have dozens of them, many made by elements for their own use. The pipeline's graph
shows the others as not exposed. The elements inside a `playbin` or a `decodebin` are never
named by you, so they are not exposed either.

`expose_all=True` of a player, or `--expose-all` on the command line, exposes every element.
Two elements of one name in different bins are keyed by their dotted path, `a.q` and `b.q`.

## Threads { #threads }

Two kinds of threads meet in a player. Your code, the model and the events run on the asyncio
loop. GStreamer runs the pipeline on its own streaming threads, and calls into Python from
them:

| Thread | Runs | Meets the loop |
|--------|------|----------------|
| The asyncio loop | your code, the model, the events, the handlers, the features, the HTTP server | it is the loop |
| GStreamer's streaming threads | pad probes, signal handlers, property notifications | they hand work over with `call_soon_threadsafe` |
| A short-lived `mixtape` thread | `set_state`, and sending an EOS | the loop awaits its result |

- **The bus** is read on the loop: the player adds the file descriptor of the bus to the loop as
  a reader, and handles every message there. The events, the handlers added with
  `add_handler` and the waits for a state or an EOS all run on the loop, never on a GStreamer
  thread. The descriptor is one reason mixtape does not run on Windows.
- **A callback of the pipeline** runs on whatever thread GStreamer calls it from: a pad probe, a
  signal handler, a `notify::` of a property. The player's own ones only read what changed and pass
  it to the loop with `call_soon_threadsafe`, so a `PropertyChanged` event is published on the
  loop although the property changed on a streaming thread.
- **A change of state**, and the sending of an EOS, run on a short-lived daemon thread, and the
  loop awaits it. A streaming thread that hangs holds the locks of the pipeline, and a call from
  the loop would hang the loop with it; this way Ctrl-C and the other tasks keep working, and
  a change to READY or NULL that takes longer than `state_timeout` is reported as stuck.
- **A command** and the HTTP server run on the loop. A sync command blocks it while it runs, so
  keep it short. The server has no threads of its own.

The rule for your code follows. On the loop you can use the player freely. From a callback of a
pipeline, hand over to the loop with
[`player.call_soon_threadsafe(callback, *args)`](reference.md#mixtape.Player.call_soon_threadsafe),
or with
[`player.run_coroutine_threadsafe(coroutine)`](reference.md#mixtape.Player.run_coroutine_threadsafe)
when you need a result. `notify` publishes an event, so it is for the loop thread only. The
[test card](library.md#subclass-player) counts its frames this way.

## Features { #features }

A feature is a plugin of a player. A [`Feature`](reference.md#mixtape.Feature) subclass adds
commands and properties to the model as `feature.name`, so they show up in `mixtape inspect`,
the HTTP API and the control page like the player's own, and it runs code around the player's
life: its hooks are called in this order, as [Write a feature](library.md#write-a-feature)
explains.

- Features are found by name in the `mixtape.features` entry point group: `leaks`, `perf` and
  `tracers` are built in, and `gst:NAME` makes any GStreamer tracer a feature, with no entry
  point. They are enabled with `--feature NAME[,KEY=VALUE]` or the `features` argument of a
  player, and a package of your own adds one with its own entry point.
- `prepare` runs before GStreamer is initialised: the place to set an environment variable,
  such as `GST_TRACERS`.
- `initialised` runs once GStreamer is initialised (`Gst.init`), and before the pipeline is built
  from a description: the place to look at what GStreamer created, such as a tracer.
- `check`, `attach` and `detach` run when the player is built, when it is set up on its loop and
  when it is torn down.

### Tracers: once per process { #tracers-once-per-process }

GStreamer creates its tracers when it initialises, from the `GST_TRACERS` environment variable,
and it does so once per process. A tracer that does not exist then does not exist later:
setting `GST_TRACERS` after `Gst.init` does nothing. Four things follow:

- `perf`, `leaks`, `tracers` and `gst:NAME` need the tracers, so they must come **first in the
  process**. `mixtape run`, `Player.from_description` and `pytest --mixtape-perf` set
  `GST_TRACERS` (in `prepare`) before they initialise GStreamer; `Player(pipeline)` cannot, as
  its pipeline already exists. A program that initialised
  GStreamer first, or imported something that did, has no tracers, and the feature fails with an
  error that says so ([Troubleshooting](troubleshooting.md#gst-tracers)).
- The tracers belong to the process, not to a player. Two players in one process share one set,
  so with `perf` on both, each records both pipelines ([Two
  pipelines](two-pipelines.md#what-mixtape-does-across-two-players)). Run two processes to
  measure two pipelines apart.
- A tracer is read by one feature. A tracer that `perf` reads is refused as `gst:NAME` beside it,
  and `gst:leaks` is refused for good: `leaks` has its own feature.
- With `--mixtape-perf`, the pytest plugin starts the tracers before pytest reads the first
  `conftest.py`, and stops with a usage error when something initialised GStreamer earlier.

[First in the process](perf.md#first-in-the-process) has the program that fails and its message.

## Interfaces { #interfaces }

Nothing below is written for a player. Each reads the model, so a player of yours, or a bare
description, gets them all:

- **The command line.** `mixtape inspect` prints the model, with `--json` as JSON.
  `mixtape run --set PATH=VALUE` sets a property of it before playing, validated like any other
  value, and `--feature` enables features. See [Command line](cli.md).
- **The REST API and the WebSocket.** `--http` (or [`serve()`](http.md#in-code) in code) runs a
  FastAPI app in the player's loop: `GET /api/model` is `describe()`, a `PATCH` sets properties,
  `POST /api/commands/NAME` calls a command, and `/api/events` streams the events. See
  [HTTP API](http.md).
- **The control page.** `GET /` is a page that reads `/api/model` and the events: a form for each
  command, a control for each property of the right type, and the pipeline graph, which follows
  the pipeline as it changes. See [The control page](http.md#the-control-page).

The pytest plugin is not made from the model: it measures every test of a suite with the
tracers, with or without a `Player`, and fails a test over its budget. See
[The recorder as a test suite](perf.md#the-recorder-as-a-test-suite).
