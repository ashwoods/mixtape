# Agents

Notes for coding agents working in this repository. Setup, checks and releasing are in
[CONTRIBUTING.md](CONTRIBUTING.md).

- The default branch is `main`.
- The command line is built with Typer, pinned `typer>=0.29,<0.30` in `pyproject.toml`: not
  Click, not argparse. `mixtape/cli.py` imports Typer's private `typer._click`, so check it
  whenever Typer is upgraded (`tests/test_typer.py` does).
- Release only when the maintainer says so. Never tag, publish or bump the version to a
  release version on your own.
- Don't file bug reports upstream (GStreamer, gst-plugins-rs, PyGObject, …) unless asked.
  Patch or work around the problem here, and note it.
- A test may be flaky only for a bound on time, latency or CPU: mark it
  `@pytest.mark.flaky` and check the bound with `tests.flaky.timing()`, which reports an
  XFAIL. The shape of an output and behaviour are never flaky. See "Tests that may be
  flaky" in CONTRIBUTING.md.
- The test suite runs every command and program in `docs/` and the README
  (`tests/doc_samples.py`, `tests/samples/`). A new snippet needs its sample.
- Run the checks with `scripts/check` (`--lint`, `--tests`, `--docs`), or with
  `scripts/check-linux` for Linux in Docker.
