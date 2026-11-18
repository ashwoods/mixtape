"""Print the worst subject of the main metrics of a run that `--feature perf,out=DIR` recorded.

python3 examples/perf_worst.py /tmp/perf-slow
"""

# --8<-- [start:worst]
import json
import sys
from pathlib import Path
from typing import Any

METRICS = ["element.latency", "pipeline.latency", "queue.fill", "process.cpu", "thread.cpu"]


def show(value: float, unit: str) -> str:
    if unit == "ns":
        return f"{value / 1e9:.1f}s" if value >= 1e9 else f"{value / 1e6:.0f}ms"
    if unit == "ratio":
        return f"{value:.0%}"
    return f"{value:.0f}%"  # a CPU, 100 is one core


def main(folder: Path) -> None:
    summary: dict[str, Any] = json.loads((folder / "summary.json").read_text())
    print(f"{'metric':<18}{'subject':<10}{'p95':>8}{'max':>8}")
    for name in METRICS:
        metric = summary["metrics"][name]
        # the subject (an element, a thread...) with the highest p95
        subject, stats = max(metric["subjects"].items(), key=lambda item: item[1]["p95"])
        p95, high = (show(stats[stat], metric["unit"]) for stat in ("p95", "max"))
        print(f"{name:<18}{subject:<10}{p95:>8}{high:>8}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
# --8<-- [end:worst]
