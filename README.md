# mixtape: awesome mix vol. 1 🎵📼 

![Pypi version] ![Pypi license] ![Python versions] ![Build Status]

|[●▪▪●]| mixtape is a Python GStreamer application framework.

**This project is in pre-alpha and the API is most likely to change**

**Documentation: [shashi.to/mixtape](https://shashi.to/mixtape/)** · [Changelog](https://shashi.to/mixtape/changelog/)

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
  a REST and WebSocket API) interfaces, started from a pipeline description.
* Pluggable features: `leaks`, `tracers` and `perf` are built in, and `gst:NAME` makes any
  GStreamer tracer one.
* Pipeline metrics from GStreamer's own tracers, live, in a report, or for every test of a
  pytest suite, with budgets and baselines.

## Install

`mixtape` needs Python 3.12 or newer, GStreamer and PyGObject (`gi`). The last two come from
your system, not from PyPI:

```console
$ brew install gstreamer    # macOS
$ sudo apt install python3-gi python3-gst-1.0 python3-venv gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good    # Debian/Ubuntu
```

Make a venv from a Python that has the bindings: `python3 -c 'import gi'` must succeed
(Homebrew's is `/opt/homebrew/bin/python3`, Ubuntu's is `/usr/bin/python3`).
`--system-site-packages` lets the venv see them, and the `http` extra adds the control page:

```console
$ python3 -m venv --system-site-packages .venv && . .venv/bin/activate
$ pip install 'mixtape[http]>=0.8'
```

To run the examples of the documentation, clone the repository; its commands run from that
folder:

```console
$ git clone https://github.com/ashwoods/mixtape
$ cd mixtape
```

## Quick start

Play a pipeline, and control it from a browser with live metrics:

```console
$ mixtape run "videotestsrc num-buffers=60 ! fakesink" && echo done
done
```

```console
$ mixtape run --http 8047 --feature perf "videotestsrc is-live=true ! queue name=q ! autovideosink"
Serving on http://127.0.0.1:8047
```

Open that address. The [documentation](https://shashi.to/mixtape/) covers the command line,
writing an application with `Player`, the HTTP API, measuring performance, a WebRTC client and
pipelines that talk to each other. Every command and snippet in it runs in the test suite,
except the installs and the commands that need a camera, a window or a server of yours, which
the page says.

## Development

[CONTRIBUTING.md](https://github.com/ashwoods/mixtape/blob/main/CONTRIBUTING.md) has what to install and how the checks work. In short, with
[uv] and GStreamer installed:

```console
$ scripts/check
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
[GObject]: https://docs.gtk.org/gobject/
[GStreamer tutorials]: https://gstreamer.freedesktop.org/documentation/tutorials/index.html
[Pypi version]: https://img.shields.io/pypi/v/mixtape.svg
[Pypi license]: https://img.shields.io/pypi/l/mixtape.svg
[Python versions]: https://img.shields.io/pypi/pyversions/mixtape.svg
[Build Status]: https://github.com/ashwoods/mixtape/actions/workflows/ci.yml/badge.svg?branch=main
