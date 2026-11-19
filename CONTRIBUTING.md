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

Ubuntu does not package the Rust plugins of gst-plugins-rs: the perf tracers (`queue-levels`,
`buffer-lateness`, `pad-push-timings`) and `webrtcsink` and `webrtcsrc`. Homebrew's `gstreamer`
includes them. Without them, the tests that need them skip.

On Debian/Ubuntu, `scripts/build-rs-plugins` builds them, as CI does. It installs nothing, so
you need cargo (Rust 1.92 or newer), git, a C toolchain, pkg-config and the GStreamer
development packages first. The build takes minutes. Then put its folder on `GST_PLUGIN_PATH`:

```console
$ sudo apt install libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer-plugins-bad1.0-dev
$ scripts/build-rs-plugins ~/.cache/gst-rs-plugins
$ export GST_PLUGIN_PATH=~/.cache/gst-rs-plugins
```

[A WebRTC client](docs/webrtc.md#install) has the same steps and a check that the elements are
found.

## Run the checks

`scripts/check` runs every check: ruff lint, ruff format, mypy, pytest, and the documentation's
commands and build:

```console
$ scripts/check
```

To run a part of it, put group flags before any pytest arguments. `--lint` runs ruff check, ruff
format --check and mypy. `--tests` runs pytest without the documentation's tests
(`tests/test_doc_samples.py` and `tests/test_docs.py`). `--docs` runs only those two: the
documentation's commands and its build. With no flag, all three run; flags combine:

```console
$ scripts/check --lint --docs
```

The remaining arguments go to pytest: `scripts/check -k eos`, or `scripts/check --tests -k eos`.

pytest runs on up to 4 xdist workers. For one worker, for example with `--pdb` or `-s`, pass
`-n 0`: `scripts/check --tests -n 0 -s -k eos`. The samples in `perf.md` and
`tests/test_examples_perf.py` always run one after another on one worker (xdist group `perf`),
because the x264 encodes would slow each other down.

The first run creates `.venv` from the Python that has `gi` (Homebrew's `python3`, or
`/usr/bin/python3`; set `MIXTAPE_PYTHON` to choose another) with `--system-site-packages`, so
the venv sees PyGObject and GStreamer's Python overrides, then `uv sync`s the dev dependencies.

After that, `uv run pytest`, `uv run ruff format` and `uv run mypy` work on their own, unless
`UV_PYTHON` or a global `uv python pin` asks for another Python: then uv replaces the venv, and
`scripts/check` guards against that. To switch to another Python, `rm -rf .venv` and run
`scripts/check` again.

`uv run mkdocs serve` serves the documentation at http://127.0.0.1:8000 while you edit it.

## Tests that may be flaky

Some tests bound a measured time, latency or CPU share, and a busy machine can break the bound
whatever the code does. Such a test is marked `@pytest.mark.flaky(reason=...)` and checks its
bounds with `tests.flaky.timing(ok, message)`, after its other checks. A bound that does not hold
is reported as an xfail, not a failure: `x` in the progress, and a line
`XFAIL ... - flaky, <reason>: <message>` in the summary. On GitHub Actions it is also a warning
annotation. Any other failure of the test still fails, and so does `timing()` in a test without
the marker.

A doc sample sets `flaky=` and `out_of_time=` on its `Sample`. The page's block is still checked
strictly; only the run's timing bounds, a run over its timeout, or the program printing its
out-of-time line are an xfail.

These tests may be flaky:

- The `record-and-read`, `compare`, `suite`, `budget-fails` and `baseline` samples of `perf.md`,
  and `tests/test_examples_perf.py`: the latency and CPU of x264 encodes of a live source depend
  on the load.
- The `demo` sample of `webrtc.md` and the call tests of `tests/test_examples_webrtc.py`: a call
  has 30 s to get going.
- `tests/test_pytest_plugin.py::test_a_budget_fails_the_test_that_is_over_it`: the budget of
  `test_generous`.
- `tests/test_threads.py`: the CPU of the threads adding up to the process's within 25%.
- `tests/test_failures.py`: the half-second bounds on the loop's answers.
- `tests/test_gst_c_crash.py`: the hang ending within 10 s.

A new test may use the mechanism only for a bound on what the machine's speed decides, never for
the shape of an output or for behaviour. When a run shows an XFAIL `flaky` line and the machine
is not busy, look at it: the bound may have broken because of the code.

## Without a native GStreamer

Run the same checks in Docker (Ubuntu 24.04), with the same arguments as `scripts/check`:

```console
$ scripts/check-linux
$ scripts/check-linux --tests -k eos
```

It builds one image, `mixtape-check`, runs it with `--rm`, and then removes the images that a
rebuild left dangling. It warns when the disk has less than 10 GB free.

The image has only Ubuntu's own GStreamer packages, without the Rust plugins, so the tests that
need them skip there. CI's `rs` job, which builds them, and its macOS job run everything.

## gst-c-crash

`tests/test_gst_c_crash.py` runs `mixtape run` against the failing elements of
[gst-c-crash](https://github.com/ashwoods/gst-c-crash) and skips unless they are found. Build it
and run:

```console
$ GST_PLUGIN_PATH=<build>/gst scripts/check
```

Several folders join with `:`, for example the Rust plugins and gst-c-crash:
`GST_PLUGIN_PATH=~/.cache/gst-rs-plugins:<build>/gst`.

## Documentation

Every command and program in `docs/` and the README runs in the test suite: see
`tests/doc_samples.py` for the rules, and `tests/samples/` for the sample of each page.

## Releasing

For the maintainer. Release only when you decide to. The steps, as 0.7.0 and 0.8.0 went:

1. Set `version` in `pyproject.toml` from the dev version (`0.8.1.dev0`) to the release version,
   for example `0.9.0`, and run `uv lock`, which sets it in `uv.lock` too. `release.yml` fails
   unless the tag is that version.
2. In `Changelog.md`, replace the heading `## X.Y.Z (unreleased)` with the release and its date,
   as in `## 0.8.0 (2026-11-17)`.
3. Land both on `main`, push, and wait for CI to pass on that commit. `scripts/ci-status`
   prints one line per job of its CI and docs runs, and exits 0 when every job passed, 1 when
   one failed and 8 while one is still running:

   ```console
   $ until scripts/ci-status <sha>; [[ $? != 8 ]]; do sleep 30; done
   ```

4. Tag that commit, with no `v` (`0.7.0`, `0.8.0`), and push the tag:

   ```console
   $ git tag -a 0.9.0 -m "mixtape 0.9.0" <sha> && git push origin 0.9.0
   ```

5. Create the GitHub release from the Changelog section: the awk line writes the lines between
   the release's heading and the previous release's.

   ```console
   $ awk '/^## 0.9.0/{f=1;next} /^## 0.8.0/{f=0} f' Changelog.md > notes.md
   $ gh release create 0.9.0 --title "0.9.0" --notes-file notes.md --verify-tag
   ```

6. Publishing the release runs `release.yml`, with three jobs in a row:
   - `ci` calls `ci.yml` on the tag: the release goes no further unless every check passes.
   - `dist` checks that `uv version --short` is the tag, builds the wheel and the sdist with
     `uv build`, attests their provenance, and attaches them to the release.
   - `pypi` uploads them to PyPI by trusted publishing, in the GitHub environment `pypi`. On
     pypi.org the project's publisher is owner `ashwoods`, repository `mixtape`, workflow
     `release.yml`, environment `pypi`. No token is stored.
7. When `release.yml` has succeeded, `docs.yml` deploys the site, https://shashi.to/mixtape/,
   from the released commit (a `workflow_run` of Release), so the site never names a version
   PyPI does not have. To deploy by hand, run the docs workflow from the Actions tab
   (`workflow_dispatch`).
8. Check the release:
   - `gh attestation verify <file> --repo ashwoods/mixtape` on a wheel or sdist downloaded from
     the GitHub release;
   - pypi.org/project/mixtape lists the version;
   - the site's Overview names it.
9. Back on `main`, set `version` to the next dev version (for example `0.9.1.dev0`, then
   `uv lock`) and add a `## 0.9.1 (unreleased)` section at the top of `Changelog.md`, as
   `0.8.1.dev0` followed `0.8.0`.

[uv]: https://docs.astral.sh/uv/
