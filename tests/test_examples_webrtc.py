"""The example of docs/webrtc.md: a call between two clients, on one machine."""

import os
import signal
import socket
import subprocess
import sys

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


pytestmark = pytest.mark.skipif(_missing() is not None, reason=_missing() or "")


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
    out = run_example("webrtc_client.py", "--demo", "--server", f"ws://127.0.0.1:{_free_port()}")
    assert out.returncode == 0, out.stdout
    assert sorted(out.stdout.splitlines()) == [
        "alice received 60 frames from bob",
        "bob received 60 frames from alice",
    ]


def test_a_client_calls_the_host_in_another_process() -> None:
    server = f"ws://127.0.0.1:{_free_port()}"
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
        out = run_example(
            "webrtc_client.py", "--name", "guest", "--server", server, "--call", "host"
        )  # it waits for the host to publish
        assert out.returncode == 0, out.stdout
        assert out.stdout.splitlines() == ["guest received 60 frames from host"]
    finally:
        host.send_signal(signal.SIGINT)
        host.communicate(timeout=20)
