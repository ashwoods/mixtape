"""The example of docs/webrtc.md: a call between two clients, on one machine."""

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from tests.doc_samples import ROOT, SCRUBBED
from tests.examples import EXAMPLES, run_example

# webrtcsink and webrtcsrc come from gst-plugins-rs, nicesrc from libnice (webrtcbin's ICE)
NEEDED = ("webrtcsink", "webrtcsrc", "webrtcbin", "nicesrc")


def _missing() -> str | None:
    for element in NEEDED:
        found = subprocess.run(["gst-inspect-1.0", "--exists", element], capture_output=True)
        if found.returncode != 0:
            return f"needs the GStreamer element {element} (gst-plugins-rs, libnice)"
    return None


MISSING = _missing()
# a call has 30 s to get going (examples/webrtc_client.py TIMEOUT)
pytestmark = [
    pytest.mark.skipif(MISSING is not None, reason=MISSING or ""),
    pytest.mark.timeout(90),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.covers(
    "webrtc_client:publish",
    "webrtc_client:client",
    "webrtc_client:props",
    "webrtc_client:call",
    "webrtc_client:host",
    "webrtc_client:main",
)
def test_two_clients_in_one_process_each_receive_the_other() -> None:
    server = f"ws://127.0.0.1:{_free_port()}"
    out = run_example("webrtc_client.py", "--demo", "--server", server, timeout=60)
    assert out.returncode == 0, out.stdout
    # not exact, as in webrtc.md's demo: on a busy machine the teardown can warn on stderr, from
    # GLib ("poll(2) failed due to: Bad file descriptor" as a call stops) or from a signaller
    # ("Connection reset without closing handshake" as alice's server stops)
    lines = out.stdout.splitlines()
    assert "alice received 60 frames from bob" in lines, out.stdout
    assert "bob received 60 frames from alice" in lines, out.stdout


def test_a_client_without_a_signalling_server_exits_with_an_error() -> None:
    """It went on waiting after the error, until Ctrl-C."""
    server = f"ws://127.0.0.1:{_free_port()}"
    out = run_example("webrtc_client.py", "--server", server, timeout=20)
    assert out.returncode == 1, out.stdout
    assert f"No signalling server at {server}: start a client with --serve" in out.stdout


def _wait_for_port(port: int, host: subprocess.Popen[str]) -> None:
    """Wait for the host's signalling server: the guest's signaller connects only once."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and host.poll() is None:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"the host's signalling server did not listen on {port}")


def test_a_client_calls_the_host_in_another_process() -> None:
    port = _free_port()
    server = f"ws://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
    host = subprocess.Popen(
        [sys.executable, str(EXAMPLES / "webrtc_client.py"), "--serve", "--name", "host"]
        + ["--server", server],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        _wait_for_port(port, host)
        out = run_example(
            "webrtc_client.py", "--name", "guest", "--server", server, "--call", "host", timeout=60
        )  # it waits for the host to publish
        assert out.returncode == 0, out.stdout
        assert "guest received 60 frames from host" in out.stdout.splitlines(), out.stdout
    finally:
        host.send_signal(signal.SIGINT)
        try:
            host.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            host.kill()
            host.communicate()
