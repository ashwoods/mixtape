"""The samples of troubleshooting.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, run

PAGE = "troubleshooting.md"

SAMPLES: list[Sample] = [
    run(PAGE, "gi", "python3 -c 'import gi'"),
    run(PAGE, "x264enc", "gst-inspect-1.0 --exists x264enc", elements=("x264enc",), exact=True),
    run(PAGE, "port", "python3 -m http.server 8047", exact=True),
]
