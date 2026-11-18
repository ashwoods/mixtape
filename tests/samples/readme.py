"""The samples of README.md: see tests/doc_samples.py."""

from tests.doc_samples import RUST_TRACERS, Sample, not_run, output, run

PAGE = "README.md"

SAMPLES: list[Sample] = [
    not_run(PAGE, "install", "^brew install gstreamer    # macOS", "installs system packages"),
    not_run(PAGE, "pip", "pip install 'mixtape[http]'", "installs packages; the tests run in one"),
    not_run(PAGE, "clone", "git clone", "clones the repository the tests run in, and installs it"),
    run(PAGE, "first-run", 'mixtape run "videotestsrc num-buffers=60', exact=True),
    not_run(PAGE, "http-perf", "mixtape run --http 8047 --feature perf", "opens a video window"),
    output(
        PAGE,
        "http-serving",
        "Serving on http://127.0.0.1:8047",
        'mixtape run --http 8047 --feature perf "videotestsrc is-live=true ! fakesink"',
        hangs=True,
        timeout=10,
        exact=True,
        elements=RUST_TRACERS,  # perf's warning of a missing one is in the output
    ),
    not_run(PAGE, "check", "^scripts/check", "runs this test suite"),
]
