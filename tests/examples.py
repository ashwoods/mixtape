"""Running the programs in examples/ for the tests in tests/test_examples_*.py."""

import os
import subprocess
import sys
from pathlib import Path

from tests.doc_samples import ROOT, SCRUBBED

EXAMPLES = ROOT / "examples"


def run_example(
    path: str, *args: str, timeout: float = 30, env: dict[str, str] | None = None, cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    """Run `examples/<path>` with this Python and `args`, without the caller's GStreamer debug
    settings: its output (stdout and stderr together) and status."""
    clean = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
    return subprocess.run(
        [sys.executable, str(EXAMPLES / path), *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        env={**clean, **(env or {})},
    )
