# mixtape: awesome mix vol. 1 🎵📼 

![Pypi version] ![Pypi license] ![Python versions] ![Build Status]

|[●▪▪●]| A python based `gstreamer` **player**-mini-framework.

**This project is in pre-alpha and the API is most likely to change**

**Documentation: [shashi.to/mixtape](http://shashi.to/mixtape/)**

[GStreamer] is a pipeline based framework for building complex multimedia processing 
systems. It is written in C, based on [GObject], and offers several bindings in other languages,
including python. If you are unfamiliar with GStreamer, you should start with the
[GStreamer tutorials].

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

`mixtape` needs GStreamer and PyGObject (`gi`), which come from your system, not from PyPI:

```console
$ brew install gstreamer    # macOS
$ sudo apt install python3-gi python3-gst-1.0 gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good    # Debian/Ubuntu
```

Install `mixtape` into that Python, or into a venv created with `--system-site-packages` from
it; the `http` extra adds the control page:

```console
$ pip install 'mixtape[http]'
```

## Quick start

Play a pipeline, and control it from a browser at http://127.0.0.1:8080 with live metrics:

```console
$ mixtape run "videotestsrc num-buffers=60 ! fakesink" && echo done
done
```

```console
$ mixtape run --http 8080 --feature perf "videotestsrc is-live=true ! queue name=q ! autovideosink"
```

The [documentation](http://shashi.to/mixtape/) covers the command line, writing an application
with `Player`, the HTTP API, measuring performance, a WebRTC client and pipelines that talk to
each other. Every command and snippet in it runs in the test suite.

## Development

You need [uv] and GStreamer with its Python bindings. The tests of the documentation's examples
also use gst-plugins-bad, gst-plugins-ugly and libnice's GStreamer plugin:

```console
$ brew install uv gstreamer libnice-gstreamer    # macOS (Homebrew)
$ sudo apt install python3-gi python3-gst-1.0 gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-nice    # Debian/Ubuntu
```

Then run every check (ruff lint, ruff format, mypy, pytest, and the documentation's commands
and build) with:

```console
$ scripts/check
```

The first run creates `.venv` from the Python that has `gi` (Homebrew's `python3`, or
`/usr/bin/python3`; set `MIXTAPE_PYTHON` to choose another) with `--system-site-packages`,
so the venv sees PyGObject and GStreamer's Python overrides, then `uv sync`s the dev
dependencies. Arguments go to pytest: `scripts/check -k eos`. After that, `uv run pytest`,
`uv run ruff format` and `uv run mypy` work on their own, unless `UV_PYTHON` or a global
`uv python pin` asks for another Python (then uv replaces the venv; `scripts/check` guards
against that). To switch to another Python, `rm -rf .venv` and run `scripts/check` again.
`uv run mkdocs serve` serves the documentation at http://127.0.0.1:8000 while you edit it.

Without a native GStreamer, run the same checks in Docker (Ubuntu 24.04, as CI does):

```console
$ docker compose run --rm test
```

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
