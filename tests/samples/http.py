"""The samples of http.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, not_run, prints, run

PAGE = "http.md"

SAMPLES: list[Sample] = [
    not_run(PAGE, "pip", "pip install 'mixtape[http]'", "installs packages; the tests run in one"),
    run(
        PAGE,
        "serve",
        "^mixtape run --http 8047 examples/demo.py:Demo",
        hangs=True,
        timeout=6,
        exact=True,
    ),
    run(
        PAGE,
        "reads",
        "--set vol.volume=0.25",
        expect=(
            '{"name":"Player","features":[],"commands":{"pause":{"name":"pause","description"',
            '"links":[{"source":"audiotestsrc0","source_pad":"src","sink":"vol"',
            '{"qos":false,"mute":false,"volume":0.25,"volume-full-range":0.25}',
            '{"state":"playing","position":',
        ),
    ),
    run(PAGE, "patch-and-pause", '"volume": 0.5, "mute": true', exact=True),
    run(PAGE, "alias-and-command", "shout", exact=True),
    run(
        PAGE,
        "errors",
        '{"volume": 11}',
        expect=(
            'Invalid value for \'vol.volume\': Input should be less than or equal to 10","errors"',
            '"errors":[{"type":"less_than_equal"',
            """{"detail":"Unknown element 'nosuch'"} 404""",
            """{"detail":"'state' is read-only"} 422""",
            "Invalid arguments for 'seek': position: Input should be greater than or equal to 0",
            '{"detail":"EOS needs a playing pipeline that is not at its EOS"} 409',
        ),
    ),
    run(PAGE, "pipeline-error", "missing.mp4", exact=True),
    prints(
        PAGE,
        "events-client",
        "examples/http_events.py",
    ),
    prints(
        PAGE,
        "events-shape",
        "examples/http_shape.py",
    ),
    run(PAGE, "bind-warning", "0.0.0.0:8047", exact=True),
    run(
        PAGE,
        "origin-and-host",
        "evil.example",
        expect=(
            """Origin 'http://evil.example' does not match Host '127.0.0.1:8047'"} 403""",
            """{"detail":"Host 'evil.example' is not allowed"} 403""",
            '{"state":"playing","position":',
        ),
    ),
    prints(
        PAGE,
        "serve-in-code",
        "examples/http_serve.py",
    ),
]
