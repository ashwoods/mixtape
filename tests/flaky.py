"""Bounds on what a test measures (a latency, a CPU share, a call that has seconds to start),
which a busy machine can break whatever the code does: CONTRIBUTING.md lists the tests that have
them, and why.

Such a test is marked `@pytest.mark.flaky(reason=...)` and checks its bounds with `timing()`,
after everything else it checks. A bound that does not hold raises `Timing`, which tests/conftest.py
reports as an xfail with the bound's message, so the run shows it and does not fail; any other
failure of the test is a failure. In a test without the marker, `Timing` is a failure too.
"""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager


class Timing(AssertionError):
    """A bound on a measured time or CPU share that did not hold."""


def timing(ok: object, message: object) -> None:
    """Check a bound on what a test measured: see the module's docstring."""
    if not ok:
        raise Timing(message)


@contextmanager
def in_time() -> Iterator[None]:
    """Around a subprocess.run(timeout=...): its running over the timeout is a broken bound."""
    try:
        yield
    except subprocess.TimeoutExpired as error:
        raise Timing(str(error)) from error
