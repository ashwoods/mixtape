"""The samples of two-pipelines.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, prints, run

PAGE = "two-pipelines.md"


def separate_pads(out: str) -> None:
    """Each process reports the pads of its own pipeline."""
    producer, _, consumer = out.partition("port 8102:")
    assert '"cam:src"' in producer and '"feed:src"' not in producer, out
    assert '"feed:src"' in consumer and '"conv:src"' in consumer, out
    assert '"cam:src"' not in consumer, out


SAMPLES: list[Sample] = [
    prints(
        PAGE,
        "inter",
        "examples/inter_restart.py",
        elements=("intervideosink", "intervideosrc"),
    ),
    prints(
        PAGE,
        "proxy",
        "examples/proxy_link.py",
        elements=("proxysink", "proxysrc"),
    ),
    run(
        PAGE,
        "unixfd",
        "mktemp -d",
        expect=("port 8101:", "port 8102:"),
        check=separate_pads,
        check_shown=True,
        elements=("unixfdsink", "unixfdsrc"),
        needs=("curl",),
        timeout=30,
    ),
    prints(
        PAGE,
        "two-players",
        "examples/two_players.py",
    ),
]
