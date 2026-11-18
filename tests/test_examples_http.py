"""The examples of docs/http.md."""

import pytest

from tests.examples import run_example


@pytest.mark.covers("http_serve:serve")
def test_serve_a_player_and_set_a_property_over_http() -> None:
    out = run_example("http_serve.py")
    assert out.returncode == 0, out.stdout
    assert out.stdout.splitlines() == [
        "serving a player named Player",
        "the server says {'volume': 0.5}",
        "the player says 0.5",
    ]


@pytest.mark.covers("http_events:client")
def test_websocket_replies_and_events() -> None:
    out = run_example("http_events.py")
    assert out.returncode == 0, out.stdout
    assert out.stdout.splitlines() == [
        "first: model",
        "first: values",
        "first: graph",
        "reply 1 {'vol.volume': 0.5}",
        "reply 2 {'status': 404, 'detail': \"Unknown command 'no-such-command'\", "
        "'code': 'unknown_name', 'hint': None}",
        "reply 3 None",
        "property [('vol.volume', 0.5)]",
        "states ['ready', 'paused', 'playing']",
    ]


@pytest.mark.covers("http_shape:watch")
def test_websocket_follows_a_pipeline_that_changes_shape() -> None:
    out = run_example("http_shape.py")
    assert out.returncode == 0, out.stdout
    assert out.stdout.splitlines() == [
        "at first, wavparse0 is in the graph: False",
        "graph: wavparse0 is in the graph, inside dec",
        "model: wavparse0 is exposed, with {'wavparse0.ignore-length': False}",
    ]
