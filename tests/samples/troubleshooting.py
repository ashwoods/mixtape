"""The samples of troubleshooting.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, run

PAGE = "troubleshooting.md"

SAMPLES: list[Sample] = [
    run(PAGE, "gi", "python3 -c 'import gi; gi.require_version("),
    run(PAGE, "x264enc", "gst-inspect-1.0 --exists x264enc", elements=("x264enc",), exact=True),
    run(PAGE, "port", "python3 -m http.server 8047", exact=True),
    run(
        PAGE,
        "cannot-reach",
        "^mixtape get 8047 vol.volume; echo",
        # the reason after the URL is the system's: [Errno 61] on macOS, 111 on Linux
        expect=(
            "error: cannot reach http://127.0.0.1:8047: [Errno ",
            "] Connection refused: is `mixtape run --http PORT` running there?\nexit status 1\n1.0",
        ),
    ),
]
