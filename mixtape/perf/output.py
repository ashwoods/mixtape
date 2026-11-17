"""The artifacts of a run in a folder, for CI and the report built on them."""

import csv
import json
import shutil
from pathlib import Path
from typing import IO, Any

from . import report
from .aggregate import Totals, Window
from .metrics import METRICS, Sample


class Artifacts:
    """Write a run to `folder`, replacing what an earlier run left:

    - `samples.jsonl`: every sample as it comes, `{"t_ns", "metric", "subject", "value"}`
    - `<metric>.csv`: the samples of each metric, `t_ns,subject,value`
      (both only with `samples`, which is on unless a suite of many runs would fill the disk)
    - `windows.jsonl`: every window, as `perf.summary` publishes it
    - `summary.json`, by `finish`: the statistics of the whole run per metric and subject
    - `report.html`, by `finish`: all of it on one page that works offline (see `report`)
    """

    def __init__(self, folder: Path, samples: bool = True) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        for stale in folder.glob("*.csv"):
            if stale.stem in METRICS:
                stale.unlink()
        (folder / "samples.jsonl").unlink(missing_ok=True)
        self._samples = open(folder / "samples.jsonl", "w", encoding="utf-8") if samples else None
        self._windows = open(folder / "windows.jsonl", "w", encoding="utf-8")
        self._csv: dict[str, tuple[IO[str], Any]] = {}  # metric: (file, writer)
        self.totals = Totals()
        self.windows = 0

    def add(self, samples: list[Sample], window: Window) -> None:
        """Append the samples of a window, and the window."""
        if self._samples is not None:
            dumps = json.dumps
            self._samples.writelines(
                dumps({"t_ns": s.t_ns, "metric": s.metric, "subject": s.subject, "value": s.value})
                + "\n"
                for s in samples
            )
            for sample in samples:
                self._writer(sample.metric).writerow((sample.t_ns, sample.subject, sample.value))
        self._windows.write(window.model_dump_json() + "\n")
        for file in self._files():
            file.flush()
        self.totals.add(samples)
        self.windows += 1

    def _files(self) -> list[IO[str]]:
        files = [self._windows, *(f for f, _ in self._csv.values())]
        return files if self._samples is None else [self._samples, *files]

    def _writer(self, metric: str) -> Any:
        if (entry := self._csv.get(metric)) is None:
            file = open(self.folder / f"{metric}.csv", "w", encoding="utf-8", newline="")
            writer = csv.writer(file)
            writer.writerow(("t_ns", "subject", "value"))
            entry = self._csv[metric] = (file, writer)
        return entry[1]

    def finish(self, about: dict[str, Any]) -> dict[str, Any]:
        """Write `summary.json`, `about` the run and the statistics of each metric used,
        close the files, and write `report.html` from them. The summary."""
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
        self._close()
        report.write(self.folder)
        return summary

    def discard(self) -> None:
        """Close the files and remove the folder, for a run that has nothing to show."""
        self._close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def _close(self) -> None:
        for file in self._files():
            file.close()
        self._csv.clear()
