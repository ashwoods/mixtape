"""The examples of docs/index.md."""

import pytest

from tests.examples import run_example


@pytest.mark.covers("hello:main")
def test_hello_plays_until_eos() -> None:
    out = run_example("hello.py")
    assert out.returncode == 0, out.stdout
    assert out.stdout.splitlines() == [
        "null -> ready",
        "ready -> paused",
        "paused -> playing",
        "end of stream",
    ]
