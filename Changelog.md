# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.8.1 (unreleased)

### Added
- `Player.from_description()` without a description builds the class's own `description`:
  `Testcard.from_description()`.
- `mixtape run --set` of a property the player does not have suggests the closest ones it has:
  `unknown property 'src.patern': did you mean 'src.pattern'?`.
- The WebRTC example takes `--stun URL`, a STUN server for peers behind NAT (none by default).
- The docs site has an `llms.txt`.

### Changed
- `perf` keeps 4 MiB of the debug log of each thread instead of 1 MiB: a WebRTC call logged
  more than 1 MiB on one thread between two reads and lost tracer records.

### Fixed
- `perf` gives a window shorter than half the interval, such as the one a run ends with, no CPU
  use or rates: a few milliseconds of CPU looked large in it, and `perf compare` of two runs of
  the same code listed them as regressions.
- `POST /api/commands/NAME` of an unknown command answers 404 `Unknown command 'NAME'`, as the
  WebSocket does, instead of `Not Found`; the commands of an element added later, its action
  signals, now work over REST too.
- `perf` no longer warns that it has no record of the creation of elements it would have seen
  but whose lines the debug log lost.
- The CLI says it needs PyGObject only when `gi` or GStreamer is missing, not on any
  ImportError.
- The WebRTC example without a signalling server exits with an error instead of waiting.
- The docs say that the `perf` and `leaks` tracers are one per process: two players measuring
  in one process each get the samples of both.

## 0.8.0 (2026-11-17)

### Added
- `mixtape inspect --json` and `GET /api/model` have `features`, the names of the enabled
  features.
- `mixtape --version` prints the versions of mixtape, GStreamer, PyGObject and Python, with
  `not found` for what is missing, for a bug report.
- A target can be a file: `mixtape run path/to/file.py:Class`; its folder is put on `sys.path`.
- `GraphChanged` and `ModelChanged` events: the player follows the pipeline's shape as it runs
  (elements a decodebin adds, pads, links, caps) and publishes the new `graph()`, and the new
  `describe()` when the exposed elements change, at most every `Player.shape_interval` (0.1 s).
  The commands follow too: an element added later brings its action signals, one removed
  takes them.
- The HTTP page's graph, element sections and perf measures follow those changes without a
  reload, keeping open sections and values being typed. `WS /api/events` sends `graph` and
  `model` messages on a change.
- `perf` warns about an element added while it runs that was made before it set up the debug
  log, as its measures carry a tracer's index instead of its name.
- `--feature gst:NAME[,key=value…]` (or `features=["gst:NAME"]`) makes any GStreamer tracer a
  feature with no Python of its own: its options are the tracer's parameters, its readable
  properties are `gst:NAME.property` and its action signals are commands, `gst:NAME.signal`,
  with structure results as JSON. It refuses `leaks` and the tracers `perf` reads beside it.
- The action signals of exposed elements are commands, `element.signal` (`mux.split-now`),
  with the parameters `arg1`, `arg2`... when the model supports their types.
- A documentation site, https://shashi.to/mixtape/, built with MkDocs from `docs/`: the command
  line, writing an application, the HTTP API, measuring performance with a real-world recorder,
  a WebRTC client and two pipelines that talk to each other. Its code comes from runnable
  programs in `examples/`, and every command and snippet on it runs in the test suite. A command
  is shown with its output, and a program with what it prints in `# >` comments, which the tests
  check. Each pipeline is drawn as a graph, made from `Player.graph()` and checked against it by
  the tests. It has a Troubleshooting page, an install flow that starts from a Python with the
  GStreamer bindings, and links from each API name to the API reference. The development notes of
  the README moved to `CONTRIBUTING.md`.

### Changed
- The command line is built with Typer: a usage error prints `error: …`, the usage line of the
  command and a pointer to its `--help`, and exits 2. Options go anywhere, `-v` before and after
  the command still adds up. mixtape now depends on `typer` (`<0.30`), which brings `rich`,
  `shellingham` and `annotated-doc`.
- The command line's errors start in lower case (`error: could not set the pipeline…`), and
  say what to do next where they can.
- `mixtape perf compare --json` (and `compare.json` of `--mixtape-perf-baseline`): `added` and
  `removed` are lists of objects with the keys that name what changed (`{"test"}`,
  `{"test", "metric"}`, or `{"metric", "subject"}`) and not strings of `"metric  subject"`;
  `threshold` (a fraction) is now `threshold_percent`, as `--threshold` is given; the result has
  `base` and `new`, the folders as given.
- `--feature NAME,nope=1` says which options the feature has: `feature 'perf' has no option
  'nope'; its options: out (path), interval (number > 0)`.
- `mixtape run demo:Demo extra words` is refused, as a player class takes no description words;
  it was read as a description.
- The README is an overview with a quick start; the reference moved to the documentation site.
- `WS /api/events` opens with a `model` message, then `values` and `graph`: a client that
  expects `values` first must skip the `model` one.
- `Feature.name` is an instance attribute (a `gst:` feature has its own), and
  `Feature.own_tracers` names the tracers a feature reads itself.

### Fixed
- The property changes of an exposed element come under its new key when a namesake added
  later renames it (`q` becomes `a.q`); they kept the old one.
- A pipeline that links a sometimes pad with `!` (`decodebin ! queue`, also `decodebin3`,
  `uridecodebin`, `uridecodebin3`) plays again after a stop: the player links the new pad
  to the same peer. GStreamer's parser links the first pad only, so the second play
  stopped with "not-linked".

## 0.7.0 (2026-11-17)

The first release since 0.5.1 (0.6.0 was never released). The core is rewritten as one
subclassable asyncio `Player`, with a declarative model of what it exposes, a command line,
an HTTP control page and feature plugins, among them `perf`, which measures pipelines with
GStreamer's own tracers, in a player or in a pytest suite. This release breaks the API.

### Breaking
- Python 3.12 or later (was 3.6). PyGObject and GStreamer come from the system, not from PyPI;
  the one dependency is `pydantic` (2.11 or later). `attrs` and `beppu` are no longer used.
- `AsyncPlayer`, `BasePlayer` (`mixtape.base`) and `mixtape.players` are removed: use
  `mixtape.Player`, a plain class (no longer an `attrs` one). `from_description` is a sync
  classmethod that needs no running loop; `create` is removed.
- `set_state` is a coroutine, like `ready`, `pause` and `play`: a call without `await` does
  nothing. `stop(eos=False, timeout=5.0)` replaces `stop(send_eos=True, teardown=False)`, so a
  bare `stop()` no longer sends an EOS first (pass `eos=True`); clean up with `teardown()` or
  `async with player`. `send_eos(timeout=None)` waits for the EOS message and
  returns nothing. `__del__` is removed.
- `Player.events` is a method returning an async iterator of the `mixtape.events` dataclasses
  (`StateChanged`, `Eos`, `Error`, `Warning`, `PropertyChanged`, `Position`, `Message`).
  `PlayerEvents`, `States` and the `asyncio.Event` subclasses are removed, and so are the
  `on_*` bus handler methods: use `add_handler(message_type, callback)`.
- `Player.run(description_or_pipeline, timeout=None)` is a sync classmethod that plays until
  EOS and tears down; the `call_play`, `call_pause` and `call_stop` helpers are removed.
- `PlayerAlreadyConfigured` is removed. `PlayerDescriptionError` is new, and
  `PlayerPipelineError` carries `source`, `message`, `debug` and `gerror`.
- `mixtape.features` is now the package of feature plugins (below); the HTTP interface lives
  in `mixtape.interfaces.http`.

### Added
- **Player.** `setup()` attaches to the running loop, and every async method attaches lazily.
  `teardown()` is idempotent, safe after the loop closed, and the player can be used again.
  New: `play_until_eos`, `eos`, `seek`, `add_handler`, `call_soon_threadsafe`,
  `run_coroutine_threadsafe`, `mixtape.player.ensure_init`, the `pending`, `loop`, `error`,
  `sources`, `sinks`, `elements` and `enabled_features` properties, `get_elements_by_gtype`
  and `exposed_elements`, and the `description`, `state_timeout` and `position_interval`
  class attributes.
- **Errors.** `ModelError` and its subclasses `UnknownNameError`, `InvalidValueError`,
  `NotReadableError`, `NotWritableError`, `FeatureError` and `CommandError`.
- **Bus policy.** An error is stored in `error` (the first since the pipeline left NULL),
  logged and published, sets the pipeline to NULL and reaches the awaiting caller as
  `PlayerPipelineError`; a failed start ends in NULL too. Warnings are logged; clock loss,
  latency and buffering are handled as `gst-play` does.
- **State changes on a thread.** A hung streaming thread no longer freezes the loop: a change
  down to READY or NULL that takes longer than `state_timeout` (5 s) raises
  `PlayerSetStateError` naming the elements still changing, and the player refuses new changes
  until it finishes; `teardown` leaves such a pipeline as it is and keeps it for the life of
  the process. A new state change fails the waits it ends with `PlayerSetStateError`.
  `async with player` stops the pipeline before the teardown and raises when that stop fails.
- **Model.** `describe()` returns a pydantic model, with JSON Schema types, of the player's
  commands and properties and those of its named elements (all of them with
  `expose_all=True`): types, ranges and current values. Declare your own with `prop()` (an
  alias of an element property), `@prop` (a computed one) and `@command`; use them with
  `get`, `set` (validated and converted), `invoke` and `notify`. A changed property is
  published as `PropertyChanged`, and `events(position=True)` adds `Position` events.
  `graph()` returns the elements, the links between them and their caps.
- **Command line.** `mixtape run TARGET` (also `python -m mixtape run`) plays a pipeline
  description or a `module:Class` until EOS or an error, like `gst-launch-1.0 -e`, with
  `--set PATH=VALUE`, `--feature` and `--expose-all`. Ctrl-C sends EOS, a second one stops, a
  third kills. It exits 0 on EOS, 1 on an error or a pipeline that cannot stop, 2 on bad
  usage and 130 after a second Ctrl-C. On macOS, video windows appear. `-v` and `-vv` log
  more, and `--version` prints the version. `mixtape inspect TARGET [--json]` prints the model
  without playing.
- **HTTP.** With the `http` extra (FastAPI and uvicorn, on the player's own loop),
  `mixtape run --http [HOST:]PORT` serves a control page generated from the model (transport
  and position, a form per command, a live control per property, the pipeline graph), a REST
  API (`/api/model`, `/api/graph`, `/api/commands/<name>`, `/api/elements/<element>`,
  `/api/properties`), `WS /api/events` and the OpenAPI at `/docs`. It binds to 127.0.0.1 and
  checks `Host` and `Origin`; there is no authentication. In code:
  `async with mixtape.interfaces.http.serve(player, host, port)`.
- **Feature plugins.** Subclass `mixtape.Feature` to add commands and live properties
  (`name.x`) to a player, with the GStreamer elements or tracers it `requires`, options
  validated from `__init__`, and the hooks `prepare`
  (before `Gst.init`), `initialised`, `check`, `attach` and `detach`. Enable one with the
  `features` class attribute, `features=` or `--feature name,key=value`; packages register
  their own under the `mixtape.features` entry point. Three are built in:
  - `leaks` counts the GStreamer objects alive by type from the core `leaks` tracer
    (`leaks.alive`, `leaks.checkpoint`, `leaks.reset`) and warns about those still alive once
    the pipeline is in NULL.
  - `tracers` runs GStreamer tracers, core or gst-plugins-rs, named in the syntax of
    `GST_TRACERS`, and logs them at `GST_TRACER:7`. The environment's `GST_TRACERS` and
    `GST_DEBUG` win, with a warning.
  - `perf` measures element and pipeline latency, the buffers, bitrate and interval of each
    pad, push time and lateness, queue levels and fill, and process and per-thread CPU, from
    the core tracers and, when installed, the gst-plugins-rs ones. It reads them from
    GStreamer's ring buffer logger, so no Python runs on a streaming thread. It publishes
    `perf.summary` every window, shows it live on the `--http` page (on the graph and in uPlot
    charts), and with `out=DIR` writes the samples, a CSV per metric, `summary.json` and a
    `report.html` that works offline.
- **pytest plugin.** `pytest --mixtape-perf DIR` measures every test of a GStreamer suite,
  whether it uses a `Player` or `Gst.parse_launch`: a report per test, a session
  `index.json` and `index.html`, and JUnit properties. A budget,
  `@pytest.mark.perf(pipeline_latency_p95="20ms", ...)`, fails a test over its limit, and
  `--mixtape-perf-baseline BASE` fails a session that got slower than an earlier one.
- **`mixtape perf compare BASE NEW`** compares two sessions or two runs: a p95 or max that grew
  by more than `--threshold` percent (default 50) and by more than a floor of its unit is a
  regression, and it exits 1. `--json` prints the comparison as JSON.

### Fixed
- A state wait could be satisfied by a stale state (play, stop, play).
- Pipeline errors went to the loop's exception handler and never reached callers.
- Building a player raised on Python 3.14+ outside a running loop.

### Project
- Packaging moved to `pyproject.toml` (PEP 621, hatchling), managed by uv. The wheel ships
  `py.typed` and uPlot 1.6.32 (MIT) in `mixtape/vendor/uplot`.
- Checks are ruff, mypy and pytest, run by `scripts/check`. CI moved from Travis to GitHub
  Actions (Ubuntu 24.04, Python 3.12, PyGObject 3.48), and a Docker image runs the same.
- A GitHub release gets the wheel and sdist, with build provenance attestations
  (`gh attestation verify <file> --repo ashwoods/mixtape`).

## 0.5.1 (2020-07-18)
- Remove pygobject-stubs as test dependency

## 0.5.0 (2020-07-14)
- Added typing
- Simplified event handling (Precursor for plugin based handling)
- Removed Bus wrapper
- Added basic error handling 

## 0.4.0 (2020-02-24)

- Refactored asyncplayer to use co-routines by default
- Using async bus-based events for asyncio state change 
