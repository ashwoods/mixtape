"""The examples of docs/library.md."""

import pytest

from tests.examples import run_example


def lines(path: str) -> list[str]:
    out = run_example(path)
    assert out.returncode == 0, out.stdout
    return out.stdout.splitlines()


@pytest.mark.covers("lib_lifecycle:lifecycle")
def test_play_pause_and_teardown() -> None:
    assert lines("lib_lifecycle.py") == ["playing", "paused", "null"]


@pytest.mark.covers("lib_until_eos:until_eos")
def test_play_until_eos_plays_again_from_the_end() -> None:
    assert lines("lib_until_eos.py") == [
        "run 1: playing at the end of the stream",
        "run 2: playing at the end of the stream",
    ]


@pytest.mark.covers("lib_run:run")
def test_run_plays_until_eos() -> None:
    assert lines("lib_run.py") == ["done"]


@pytest.mark.covers("lib_dynamic:build")
def test_a_player_for_a_pipeline_built_in_code() -> None:
    assert lines("lib_dynamic.py") == [
        "elements: dynamic, src, sink",
        "links: [('src', 'sink')]",
        "played",
    ]


@pytest.mark.covers("lib_errors:description", "lib_errors:start", "lib_errors:playing")
def test_errors_of_the_description_the_start_and_the_stream() -> None:
    assert lines("lib_errors.py") == [
        "Invalid pipeline 'videotestsrc ! nosuchelement ! fakesink': no element \"nosuchelement\"",
        'filesink0: Could not open file "/nonexistent/x" for writing.',
        "state: null",
        "flaky: Failed after iterations as requested.",
        "player.error is the exception: True",
        "error after playing again: None",
    ]


@pytest.mark.covers("lib_events:events")
def test_the_events_of_a_short_stream() -> None:
    assert lines("lib_events.py") == [
        "src.pattern = ball",
        "playing",
        "the position moves",
        "end of stream",
    ]


@pytest.mark.covers("lib_handler:handler")
def test_a_handler_runs_until_it_is_removed() -> None:
    assert lines("lib_handler.py") == ["a stream started", "done"]


@pytest.mark.covers("lib_shape:shape")
def test_a_branch_added_while_playing_changes_the_graph_and_the_model() -> None:
    assert lines("lib_shape.py") == ["exposed: t, main", "links: 5", "exposed: t, main, extra"]


@pytest.mark.covers("testcard:player", "testcard:main")
def test_the_test_card_player() -> None:
    assert lines("testcard.py") == [
        "commands: eos, pause, play, seek, show, stop",
        "properties: duration, frames, pattern, position, state",
        "pattern: smpte",
        "show: snow",
        "from a thread: smpte",
        "rejected: Invalid arguments for 'show': "
        "pattern: Input should be 'smpte', 'ball' or 'snow'",
        "frames: 10",
    ]


@pytest.mark.covers("heartbeat:feature", "heartbeat_demo:demo")
def test_the_heartbeat_feature() -> None:
    assert lines("heartbeat_plugin/heartbeat_demo.py") == [
        "in the model: heartbeat.reset heartbeat.beats",
        "heartbeat.beats = 3",
        "reset: None 0",
        "detached: True",
        "Invalid options for feature 'heartbeat': interval: Input should be greater than 0",
        "heartbeat needs a pipeline with a sink",
    ]
