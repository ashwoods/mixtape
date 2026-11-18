"""The samples of webrtc.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, not_run, run

PAGE = "webrtc.md"


# webrtcsink's codec discovery opens a VideoToolbox session (vtenc_h264, vtenc_h265) on macOS,
# and on a virtual Mac without Apple's scaler, as GitHub's macOS runners are, macOS itself prints
# this line on stderr (CI run 44379800678). It is not mixtape's, and not seen on a real Mac.
MACOS_VM_NOISE = "IOServiceMatchingfailed for: AppleM2ScalerParavirtDriver"


def silent(out: str) -> None:
    lines = [line for line in out.splitlines() if line.strip() != MACOS_VM_NOISE]
    assert "".join(lines).strip() == "", f"it printed:\n{out}"


SAMPLES: list[Sample] = [
    not_run(PAGE, "brew", "brew install gstreamer libnice-gstreamer", "installs system packages"),
    not_run(PAGE, "apt", "sudo apt install gstreamer1.0-plugins-bad", "installs system packages"),
    not_run(
        PAGE,
        "build-plugins",
        "scripts/build-rs-plugins ~/.cache/gst-rs-plugins",
        "builds gst-plugins-rs with cargo, which takes minutes; CI runs it",
    ),
    run(
        PAGE, "exists", "gst-inspect-1.0 --exists webrtcsink", exact=True, elements=("webrtcsink",)
    ),
    # the host publishes until it is interrupted, and says nothing
    run(
        PAGE,
        "serve",
        "python3 examples/webrtc_client.py --serve --name host",
        hangs=True,
        timeout=8,
        check=silent,
        elements=("webrtcsink", "nicesrc"),
    ),
    not_run(
        PAGE,
        "call",
        "python3 examples/webrtc_client.py --name guest --call host",
        "needs the host of the block above running in another terminal; the --demo below is "
        "the same call in one command, and tests/test_examples_webrtc.py runs both processes",
    ),
    run(
        PAGE,
        "demo",
        "python3 examples/webrtc_client.py --demo",
        # not exact: where libnice is built with UPnP, as on Ubuntu, it can warn on stderr that
        # the port it picked for its UPnP server is taken (CI run 44160582039)
        expect=("alice received 60 frames from bob", "bob received 60 frames from alice"),
        elements=("webrtcsink", "webrtcsrc", "nicesrc"),
    ),
    not_run(
        PAGE,
        "camera",
        "python3 examples/webrtc_client.py --camera",
        "needs a camera and a window",
    ),
    run(
        PAGE,
        "http",
        "Client --http 8048",
        hangs=True,
        timeout=10,
        expect=("Serving on http://127.0.0.1:8048",),
        elements=("webrtcsink", "nicesrc"),
    ),
    run(
        PAGE,
        "perf",
        "--http 8047 --feature perf",
        hangs=True,
        timeout=8,
        expect=("Serving on http://127.0.0.1:8047",),
        elements=("webrtcsink", "nicesrc", "queue-levels"),
    ),
]
