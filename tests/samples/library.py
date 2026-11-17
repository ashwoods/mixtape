"""The samples of library.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, not_run, output, prints, run

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
    prints(
        PAGE,
        "lifecycle",
        "examples/lib_lifecycle.py",
    ),
    prints(
        PAGE,
        "until-eos",
        "examples/lib_until_eos.py",
    ),
    prints(
        PAGE,
        "run",
        "examples/lib_run.py",
    ),
    prints(
        PAGE,
        "dynamic",
        "examples/lib_dynamic.py",
    ),
    prints(
        PAGE,
        "errors",
        "examples/lib_errors.py",
    ),
    prints(
        PAGE,
        "events",
        "examples/lib_events.py",
    ),
    prints(
        PAGE,
        "handler",
        "examples/lib_handler.py",
    ),
    prints(
        PAGE,
        "shape",
        "examples/lib_shape.py",
    ),
    prints(
        PAGE,
        "testcard",
        "examples/testcard.py",
    ),
    run(PAGE, "inspect-testcard", "mixtape inspect examples/testcard.py:Testcard", exact=True),
    prints(
        PAGE,
        "heartbeat",
        "examples/heartbeat_plugin/heartbeat_demo.py",
    ),
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
