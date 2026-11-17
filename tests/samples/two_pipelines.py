"""The samples of two-pipelines.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, run

PAGE = "two-pipelines.md"


def separate_pads(out: str) -> None:
    """Each process reports the pads of its own pipeline."""
    producer, _, consumer = out.partition("consumer, :8102:")
    assert '"cam:src"' in producer and '"feed:src"' not in producer, out
    assert '"feed:src"' in consumer and '"conv:src"' in consumer, out
    assert '"cam:src"' not in consumer, out


SAMPLES: list[Sample] = [
    run(
        PAGE,
        "inter",
        "python3 examples/inter_restart.py",
        exact=True,
        elements=("intervideosink", "intervideosrc"),
    ),
    run(
        PAGE,
        "proxy",
        "python3 examples/proxy_link.py",
        exact=True,
        elements=("proxysink", "proxysrc"),
    ),
    run(
        PAGE,
        "unixfd",
        "mktemp -d",
        expect=("Serving on http://127.0.0.1:8101", "Serving on http://127.0.0.1:8102"),
        check=separate_pads,
        check_shown=True,
        elements=("unixfdsink", "unixfdsrc"),
        needs=("curl",),
        timeout=30,
    ),
    run(PAGE, "two-players", "python3 examples/two_players.py", exact=True),
]
