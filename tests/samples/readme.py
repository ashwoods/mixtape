"""The samples of README.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, not_run, run

PAGE = "README.md"

SAMPLES: list[Sample] = [
    not_run(PAGE, "install", "^brew install gstreamer    # macOS", "installs system packages"),
    not_run(PAGE, "pip", "pip install 'mixtape[http]'", "installs packages; the tests run in one"),
    run(PAGE, "first-run", 'mixtape run "videotestsrc num-buffers=60', exact=True),
    not_run(
        PAGE,
        "http-perf",
        "mixtape run --http 8080 --feature perf",
        "serves until Ctrl-C and opens a video window; docs/http.md tests the page",
    ),
    not_run(PAGE, "dev-install", "brew install uv gstreamer libnice-gstreamer", "installs"),
    not_run(PAGE, "check", "^scripts/check", "runs this test suite"),
    not_run(PAGE, "docker", "docker compose run --rm test", "runs this test suite in Docker"),
]
