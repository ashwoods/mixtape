# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.7.0 (unreleased)

The core is rewritten as one subclassable asyncio `Player` in `mixtape/player.py`.
This release breaks the API.

- **Breaking:** `BasePlayer` (`mixtape.base`), `AsyncPlayer` and `mixtape.players` are removed;
  use `mixtape.Player`.
- **Breaking:** `Player` is a plain class, no longer an `attrs` class. `from_description` is
  a sync classmethod; `create` is removed. `attrs` and `beppu` are no longer dependencies, and
  mixtape has none: PyGObject comes from the system.
- **Breaking:** `Player.events` is now a method returning an async iterator of `mixtape.events`
  dataclasses (`StateChanged`, `Eos`, `Error`, `Warning`, `Message`). `PlayerEvents`,
  `States` and the asyncio event subclasses are removed.
- **Breaking:** `PlayerAlreadyConfigured` is removed; `PlayerDescriptionError` is new, and
  `PlayerPipelineError` carries `source`, `message`, `debug` and `gerror`.
- **Breaking:** `stop(eos=False, timeout=5.0)` replaces `stop(send_eos, teardown)`; use
  `teardown()` or `async with player` to clean up. `__del__` is removed.
- **Breaking:** `Player.run(description_or_pipeline, timeout=None)` is a sync classmethod that
  plays until EOS and tears down; `autoplay` and the `call_x` helpers are gone.
- New: `setup()` attaches to the running loop, and every async method attaches lazily.
  `teardown()` is idempotent, safe after the loop closed, and the player can be re-used.
- New: the `mixtape run "<pipeline>"` command (also `python -m mixtape`) plays a pipeline until EOS
  or an error like `gst-launch-1.0 -e`, and makes video windows appear on macOS.
- New: `add_handler`, `call_soon_threadsafe`, `run_coroutine_threadsafe`, `ensure_init`,
  and the `pending`, `loop` and `error` properties.
- New default bus policy: errors are stored, logged and published, set the pipeline to NULL, and
  reach the awaiting caller; warnings are logged; clock loss, latency and buffering are handled
  as `gst-play` does.
- Fix: a state wait could be satisfied by a stale state (play, stop, play).
- Fix: pipeline errors were raised into the loop's exception handler and never reached callers.
- Fix: EOS flags were never reset, so a second `play_until_eos` returned at once.
- Fix: teardown from `__del__` failed outside a running loop.
- Fix: `Player(...)` raised on Python 3.14+ outside a running loop.
- Fix: a hung streaming thread froze the loop, so neither Ctrl-C nor `--http` worked: state
  changes run on a thread, and one down to READY or NULL that takes longer than
  `state_timeout` (5 s) raises `PlayerSetStateError` naming the elements still changing; the
  player refuses changes until it finishes, and `teardown` leaves such a pipeline.
- Fix: a failed stop exited 0, and left the bus flushing so the next play hung; failed state
  changes name the elements that failed them, when those still say so. `mixtape run` exits 1 when the pipeline cannot
  stop. `async with player` stops the pipeline before the teardown and raises a stop that fails;
  the waits it ends get `PlayerSetStateError`, no longer `CancelledError`.
- **Behaviour change:** a failed start sets the pipeline to NULL, as an error does: GstBin
  left it half way up, reported as `paused`. An element that fails silently (posts no ERROR)
  may still not be named, since GStreamer keeps no trace of it.
- **Behaviour change:** the `eos` command (`player.eos()`) waits 5 s by default (`timeout`,
  was none) and raises `CommandError` (409 over HTTP) when no EOS comes, or at once while a
  change is stuck.
- Fix: `error` (and the error a failed start raises) is the first error since the start, the
  cause, not a fallback posted after it (basesrc follows its subclass's error with its own).
- Fix: a pipeline that teardown leaves outside NULL is kept for the life of the process on
  purpose, instead of GStreamer printing a CRITICAL as it disposes it.
- New: the `leaks` feature warns about the objects still alive once the pipeline is in NULL;
  `teardown` detaches the features after setting NULL. A page connecting over `--http` gets
  the error that stopped the pipeline. `error` is cleared when the pipeline starts again.
- Packaging moved to `pyproject.toml` managed by uv; Python 3.12+
- Checks are ruff, mypy and pytest, run by `scripts/check`; CI moved to GitHub Actions
- Docker test image based on Ubuntu 24.04 packages
- Fix `Player.play_until_eos` on Python 3.11+

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
