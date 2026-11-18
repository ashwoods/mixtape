"""`mixtape.spec` and `mixtape.perf` are the parts of mixtape that import without GStreamer: they
may not import `gi`, the features or the player (the `perf` feature and the collector are under
`mixtape.features.perf`)."""

import pkgutil
import subprocess
import sys

import mixtape.perf

CHECK = """
import importlib, sys

for name in sys.argv[1:]:
    importlib.import_module(name)
found = sorted(m for m in sys.modules if m.split(".")[0] == "gi"
               or m.startswith(("mixtape.features", "mixtape.player")))
assert not found, found
"""


def test_spec_and_perf_import_without_gi_features_or_player() -> None:
    modules = ["mixtape.spec", "mixtape.perf"]
    modules += [m.name for m in pkgutil.iter_modules(mixtape.perf.__path__, "mixtape.perf.")]
    result = subprocess.run(
        [sys.executable, "-c", CHECK, *modules], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
