"""Build the documentation site, so a snippet that points at a missing file or section, or a
section no test covers, fails the tests, not only the docs workflow."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.timeout(120)
def test_docs_build(tmp_path: Path) -> None:
    pytest.importorskip("mkdocs", reason="mkdocs-material is not installed")
    out = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(tmp_path / "site")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=110,
        env={"NO_MKDOCS_2_WARNING": "1", "PATH": os.environ["PATH"]},
    )
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "site" / "cli" / "index.html").exists()
    shutil.rmtree(tmp_path / "site")
