"""The samples of index.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, not_run, output, prints, run

PAGE = "index.md"

SAMPLES: list[Sample] = [
    not_run(PAGE, "brew", "brew install gstreamer", "installs system packages"),
    not_run(PAGE, "apt", "sudo apt install python3-gi", "installs system packages"),
    run(PAGE, "gi", "python3 -c 'import gi; gi.require_version("),
    not_run(PAGE, "pip", "pip install 'mixtape[http]'", "creates a venv and installs packages"),
    not_run(PAGE, "clone", "git clone", "clones the repository the tests run in, and installs it"),
    run(PAGE, "first-run", 'mixtape run "videotestsrc num-buffers=60', exact=True),
    run(PAGE, "failure", 'mixtape run "videotestsrc ! nosuchelement"', exact=True),
    not_run(PAGE, "http", "mixtape run --http 8047", "opens a video window"),
    output(
        PAGE,
        "http-serving",
        "Serving on http://127.0.0.1:8047",
        'mixtape run --http 8047 "videotestsrc is-live=true ! fakesink"',
        hangs=True,
        timeout=6,
        exact=True,
    ),
    prints(
        PAGE,
        "hello",
        "examples/hello.py",
    ),
]
