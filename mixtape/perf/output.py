"""The artifacts of a run in a folder, for CI and the report built on them."""

import csv
import json
from pathlib import Path
from typing import IO, Any

from .aggregate import Totals, Window
from .metrics import METRICS, Sample


class Artifacts:
    """Write a run to `folder`, replacing what an earlier run left:

    - `samples.jsonl`: every sample as it comes, `{"t_ns", "metric", "subject", "value"}`
    - `<metric>.csv`: the samples of each metric, `t_ns,subject,value`
    - `windows.jsonl`: every window, as `perf.summary` publishes it
    - `summary.json`, by `finish`: the statistics of the whole run per metric and subject
    """

    def __init__(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        for stale in folder.glob("*.csv"):
            if stale.stem in METRICS:
                stale.unlink()
        self._samples = open(folder / "samples.jsonl", "w", encoding="utf-8")
        self._windows = open(folder / "windows.jsonl", "w", encoding="utf-8")
        self._csv: dict[str, tuple[IO[str], Any]] = {}  # metric: (file, writer)
        self.totals = Totals()
        self.windows = 0

    def add(self, samples: list[Sample], window: Window) -> None:
        """Append the samples of a window, and the window."""
        dumps = json.dumps
        self._samples.writelines(
            dumps({"t_ns": s.t_ns, "metric": s.metric, "subject": s.subject, "value": s.value})
            + "\n"
            for s in samples
        )
        for sample in samples:
            self._writer(sample.metric).writerow((sample.t_ns, sample.subject, sample.value))
        self._windows.write(window.model_dump_json() + "\n")
        for file in (self._samples, self._windows, *(f for f, _ in self._csv.values())):
            file.flush()
        self.totals.add(samples)
        self.windows += 1

    def _writer(self, metric: str) -> Any:
        if (entry := self._csv.get(metric)) is None:
            file = open(self.folder / f"{metric}.csv", "w", encoding="utf-8", newline="")
            writer = csv.writer(file)
            writer.writerow(("t_ns", "subject", "value"))
            entry = self._csv[metric] = (file, writer)
        return entry[1]

    def finish(self, about: dict[str, Any]) -> None:
        """Write `summary.json`, `about` the run and the statistics of each metric used,
        and close the files."""
        stats = self.totals.stats()
        summary = {
            **about,
            "windows": self.windows,
            "metrics": {
                metric: {
                    "unit": METRICS[metric].unit,
                    "description": METRICS[metric].description,
                    "subjects": {s: stat.model_dump() for s, stat in subjects.items()},
                }
                for metric, subjects in stats.items()
            },
        }
        (self.folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        for file in (self._samples, self._windows, *(f for f, _ in self._csv.values())):
            file.close()
        self._csv.clear()
