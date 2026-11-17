"""The examples of docs/two-pipelines.md."""

import gi
import pytest

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape.player import ensure_init
from tests.examples import run_example

ensure_init()


def needs(*elements: str) -> pytest.MarkDecorator:
    missing = [name for name in elements if Gst.ElementFactory.find(name) is None]
    return pytest.mark.skipif(bool(missing), reason=f"needs the GStreamer elements {missing}")


@needs("intervideosink", "intervideosrc")
@pytest.mark.covers("inter_restart:players", "inter_restart:count", "inter_restart:main")
def test_the_consumer_renders_while_the_producer_stops_and_restarts() -> None:
    out = run_example("inter_restart.py")
    assert out.returncode == 0, out.stdout
    assert out.stdout.splitlines() == [
        "producer playing: the consumer rendered more than 10 frames: True",
        "producer stopped: the consumer rendered more than 10 frames: True",
        "producer restarted: the consumer rendered more than 10 frames: True",
    ]


@needs("proxysink", "proxysrc")
@pytest.mark.covers(
    "proxy_link:link",
    "proxy_link:order",
    "proxy_link:eos",
    "proxy_link:lag",
    "proxy_link:main",
)
def test_proxy_link_shares_the_base_time_and_starts_in_order() -> None:
    out = run_example("proxy_link.py")
    assert out.returncode == 0, out.stdout
    assert out.stdout.splitlines() == [
        "the consumer waits for the producer to play: True",
        "all 50 buffers and the EOS arrive: True",
        "shared base time: first frame before the producer's age: True",
        "own base time: first frame after the producer's age: True",
    ]


@pytest.mark.covers("two_players:serve", "two_players:perf", "two_players:main")
def test_two_players_in_one_process_share_the_tracers() -> None:
    out = run_example("two_players.py", timeout=60)
    assert out.returncode == 0, out.stdout
    assert out.stdout.splitlines() == [
        "two ports: True",
        "cam's page lists: ['cam', 'q']",
        "mic's page lists: ['mic', 'q']",
        "cam's perf counted buffers of: ['cam:src', 'mic:src', 'q:src']",
        "mic's perf counted buffers of: ['cam:src', 'mic:src', 'q:src']",
    ]
