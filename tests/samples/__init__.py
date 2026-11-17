"""The samples of every page, one module per page: see tests/doc_samples.py."""

from tests.doc_samples import Sample

from . import cli, graphs, http, index, library, perf, readme, two_pipelines, webrtc

SAMPLES: list[Sample] = [
    *readme.SAMPLES,
    *index.SAMPLES,
    *cli.SAMPLES,
    *library.SAMPLES,
    *http.SAMPLES,
    *perf.SAMPLES,
    *webrtc.SAMPLES,
    *two_pipelines.SAMPLES,
    *graphs.SAMPLES,
]
