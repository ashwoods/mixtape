"""The samples of index.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, not_run, prints, run

PAGE = "index.md"

SAMPLES: list[Sample] = [
    not_run(PAGE, "brew", "brew install gstreamer", "installs system packages"),
    not_run(PAGE, "apt", "sudo apt install python3-gi", "installs system packages"),
    not_run(PAGE, "pip", "pip install 'mixtape[http]'", "installs packages; the tests run in one"),
    run(PAGE, "first-run", 'mixtape run "videotestsrc num-buffers=60', exact=True),
    prints(
        PAGE,
        "hello",
        "examples/hello.py",
    ),
]
