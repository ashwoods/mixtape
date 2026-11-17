"""The samples of library.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, not_run, output, run

PAGE = "library.md"

# What `pip install ./examples/heartbeat_plugin` leaves for `mixtape.features` to find: a
# *.dist-info folder with the entry points of the package's pyproject.toml, in a folder on
# PYTHONPATH.
ENTRY_POINTS = (
    "D=/tmp/site && mkdir -p $D/mixtape_heartbeat-0.1.dist-info"
    " && { echo '[mixtape.features]';"
    " grep '^heartbeat =' examples/heartbeat_plugin/pyproject.toml | tr -d '\"'; }"
    " > $D/mixtape_heartbeat-0.1.dist-info/entry_points.txt"
    " && printf 'Metadata-Version: 2.1\\nName: mixtape-heartbeat\\nVersion: 0.1\\n'"
    " > $D/mixtape_heartbeat-0.1.dist-info/METADATA"
    " && export PYTHONPATH=$D:examples/heartbeat_plugin && "
)
INSPECT_HEARTBEAT = (
    'mixtape inspect --feature heartbeat,interval=0.5 "videotestsrc ! fakesink" | grep heartbeat'
)

SAMPLES: list[Sample] = [
    run(PAGE, "lifecycle", "python3 examples/lib_lifecycle.py", exact=True),
    run(PAGE, "until-eos", "python3 examples/lib_until_eos.py", exact=True),
    run(PAGE, "run", "python3 examples/lib_run.py", exact=True),
    run(PAGE, "dynamic", "python3 examples/lib_dynamic.py", exact=True),
    run(PAGE, "errors", "python3 examples/lib_errors.py", exact=True),
    run(PAGE, "events", "python3 examples/lib_events.py", exact=True),
    run(PAGE, "handler", "python3 examples/lib_handler.py", exact=True),
    run(PAGE, "shape", "python3 examples/lib_shape.py", exact=True),
    run(PAGE, "testcard", "python3 examples/testcard.py", exact=True),
    run(PAGE, "inspect-testcard", "mixtape inspect testcard:Testcard", exact=True),
    run(PAGE, "heartbeat", "python3 examples/heartbeat_plugin/heartbeat_demo.py", exact=True),
    not_run(PAGE, "pip-heartbeat", "pip install ./examples/heartbeat_plugin", "installs a package"),
    not_run(
        PAGE,
        "inspect-heartbeat",
        "mixtape inspect --feature heartbeat",
        "needs the package installed, which the tests do not do to their venv; the output below"
        " is checked with its entry point on PYTHONPATH instead",
    ),
    output(
        PAGE,
        "inspect-heartbeat-output",
        "^Features: heartbeat",
        ENTRY_POINTS + INSPECT_HEARTBEAT,
        exact=True,
    ),
]
