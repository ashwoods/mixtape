# Contributing

## Set up

You need [uv] and GStreamer with its Python bindings. The tests of the documentation's examples
also use gst-plugins-bad, gst-plugins-ugly and libnice's GStreamer plugin:

```console
$ brew install uv gstreamer libnice-gstreamer    # macOS (Homebrew)
$ sudo apt install python3-gi python3-gst-1.0 gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-nice    # Debian/Ubuntu
```

## Run the checks

`scripts/check` runs every check: ruff lint, ruff format, mypy, pytest, and the documentation's
commands and build:

```console
$ scripts/check
```

Arguments go to pytest: `scripts/check -k eos`.

The first run creates `.venv` from the Python that has `gi` (Homebrew's `python3`, or
`/usr/bin/python3`; set `MIXTAPE_PYTHON` to choose another) with `--system-site-packages`, so
the venv sees PyGObject and GStreamer's Python overrides, then `uv sync`s the dev dependencies.

After that, `uv run pytest`, `uv run ruff format` and `uv run mypy` work on their own, unless
`UV_PYTHON` or a global `uv python pin` asks for another Python: then uv replaces the venv, and
`scripts/check` guards against that. To switch to another Python, `rm -rf .venv` and run
`scripts/check` again.

`uv run mkdocs serve` serves the documentation at http://127.0.0.1:8000 while you edit it.

## Without a native GStreamer

Run the same checks in Docker (Ubuntu 24.04, as CI does):

```console
$ docker compose run --rm test
```

## gst-c-crash

`tests/test_gst_c_crash.py` runs `mixtape run` against the failing elements of
[gst-c-crash](https://github.com/ashwoods/gst-c-crash) and skips unless they are found. Build it
and run:

```console
$ GST_PLUGIN_PATH=<build>/gst scripts/check
```

## Documentation

Every command and program in `docs/` and the README runs in the test suite: see
`tests/doc_samples.py` for the rules, and `tests/samples/` for the sample of each page.

[uv]: https://docs.astral.sh/uv/
