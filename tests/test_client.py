import json
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from mixtape.cli import main

PLAYER = "audiotestsrc is-live=true ! volume name=vol ! fakesink"


@pytest.fixture(scope="module")
def url() -> Iterator[str]:
    """The URL of a player that `mixtape run --http` serves."""
    argv = [sys.executable, "-m", "mixtape", "run", "--http", "0", "--json", "--timeout", "60"]
    process = subprocess.Popen([*argv, PLAYER], stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None
    try:
        yield json.loads(process.stdout.readline())["url"]
    finally:
        process.kill()
        process.wait()


def test_get_set_and_call(url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["get", url, "vol.volume"]) == 0
    assert capsys.readouterr().out == "1.0\n"
    assert main(["set", url, "vol.volume=0.5"]) == 0  # a string, converted by the server
    assert capsys.readouterr().out == ""
    assert main(["get", "--json", url, "vol.volume", "vol.mute"]) == 0
    assert json.loads(capsys.readouterr().out) == {"vol.volume": 0.5, "vol.mute": False}
    assert main(["call", url, "pause"]) == 0
    assert main(["get", url, "state"]) == 0
    assert capsys.readouterr().out == "paused\n"
    assert main(["call", "--json", url, "play"]) == 0
    assert json.loads(capsys.readouterr().out) == {"result": None}
    assert main(["get", url]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "state = playing" and "vol.volume = 0.5" in lines


def test_errors_are_the_servers(url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["get", "--json", url, "vol.volum"]) == 2
    assert json.loads(capsys.readouterr().err) == {
        "error": {
            "code": "unknown_name",
            "message": "Unknown property 'vol.volum'",
            "hint": "did you mean 'vol.volume'?",
        }
    }
    assert main(["set", "--json", url, "vol.volume=loud"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_value"
    assert main(["call", url, "paus"]) == 2
    assert capsys.readouterr().err == "error: unknown command 'paus': did you mean 'pause'?\n"
    assert main(["set", "--json", url, "vol.volume:=NaN"]) == 2  # JSON has no NaN
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"


def test_a_nan_is_an_invalid_value(url: str) -> None:
    """Python's json reads a NaN, which pydantic refuses: its error has the NaN, which the
    body has as null, not a 500."""
    body = b'{"volume": NaN}'
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(f"{url}/api/elements/vol", body, headers, method="PATCH")
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=10)
    assert refused.value.code == 422
    assert json.loads(refused.value.read())["code"] == "invalid_value"


def test_watch_prints_the_messages_of_the_socket(url: str) -> None:
    argv = [sys.executable, "-m", "mixtape", "watch", url]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None
    try:
        messages = [json.loads(process.stdout.readline()) for _ in range(3)]
        assert [m["type"] for m in messages] == ["model", "values", "graph"]
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=5) == 130
    finally:
        process.kill()
        process.wait()


def test_get_needs_no_pygobject(url: str) -> None:
    code = (
        "import sys\n"
        "sys.modules['gi'] = None\n"  # `import gi` raises ImportError
        "from mixtape.cli import main\n"
        "sys.exit(main(sys.argv[1:]))\n"
    )
    argv = [sys.executable, "-c", code, "get", url, "vol.mute"]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert (done.returncode, done.stdout, done.stderr) == (0, "false\n", "")
