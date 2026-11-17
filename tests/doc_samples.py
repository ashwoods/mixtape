"""The machinery of tests/test_doc_samples.py: the fenced blocks of the pages and the samples
that test them. The samples themselves are in tests/samples/, one module per page.

Every fenced block in README.md and docs/*.md that is not an example program (`--8<--`) is
either a command (`sh`) or an output (no language, or `text`), and every one needs a sample:

- `run`: a command block, run verbatim with `bash -c` in the repository. Its status and its
  expected strings are checked, and every expected string must also be in the output block
  that follows it (or the one `shows` names), so the output the page shows cannot drift from
  what the command prints.
- `output`: an output block with no command before it: `command` produces it.
- `not_run`: a command that is setup, needs a camera, or runs until Ctrl-C, with the reason.
- `illustrative`: an output that cannot be checked. The page must say so in the block's title.

A block that no sample matches, or two do, fails `test_every_block_is_covered`, so a sample
added to a page without a test fails the suite. Where a tool, an element or a platform is
missing the sample is skipped, one by one.

Python code on a page is always an included section of a program in examples/, which the tests
in tests/test_examples_*.py run; hooks/source_lines.py links each to its tests.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted((ROOT / "docs").glob("*.md"))
ILLUSTRATIVE = "Illustrative output"
AUTO = "auto"


# --- the blocks in the pages ------------------------------------------------------------------

FENCE = re.compile(r"^( *)(`{3,})\s*(\S*)\s*(.*)$")


@dataclass(frozen=True)
class Block:
    page: str
    line: int
    lang: str
    title: str
    text: str

    @property
    def where(self) -> str:
        return f"{self.page}:{self.line}"

    @property
    def category(self) -> str:
        """`command`, `output`, or `example` (a program included from examples/)."""
        if "--8<--" in self.text:
            return "example"
        if self.lang in ("sh", "console"):
            return "command"
        if self.lang in ("", "text"):
            return "output"
        raise AssertionError(
            f"{self.where}: a ```{self.lang} block without --8<-- is neither a command nor an"
            " output: put the code in examples/ and include a section of it"
        )


def extract(path: Path) -> list[Block]:
    page = path.name
    lines = path.read_text().splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        indent, ticks, lang, rest = m.groups()
        start = i + 1
        body = []
        i += 1
        while lines[i].strip() != ticks:
            # the blocks in tabs and list items are indented
            line = lines[i]
            body.append(line[len(indent) :] if line.startswith(indent) else line.lstrip())
            i += 1
        i += 1
        title = re.search(r'title="([^"]*)"', rest)
        blocks.append(Block(page, start, lang, title[1] if title else "", "\n".join(body)))
    return blocks


BLOCKS = [b for path in PAGES for b in extract(path)]


# --- the samples ------------------------------------------------------------------------------

Status = int | tuple[int, ...] | dict[str, int]


@dataclass
class Sample:
    page: str
    name: str
    contains: str  # a unique substring of the block; `^` starts it with it
    kind: str  # run, output, not-run, illustrative
    why: str = ""
    command: str = ""  # output: the command that produces the block
    shows: str | None = AUTO  # run: contains of the output block (AUTO: the next block)
    status: Status = 0  # an int, a tuple of ints, or a {platform: int} dict
    timeout: int = 60
    hangs: bool = False  # the command is expected to run into the timeout
    expect: tuple[str, ...] = ()  # strings in the real output and in the block shown
    exact: bool = False  # the real output is the block shown
    check: Callable[[str], None] | None = None  # asserts on the real output
    check_shown: bool = False  # and on the block shown, for numbers the page states
    only: str = ""  # "linux" or "darwin"
    needs: tuple[str, ...] = ()  # tools on PATH that must be installed
    elements: tuple[str, ...] = ()  # GStreamer elements (or tracers) that must be installed

    @property
    def id(self) -> str:
        return f"{self.page}:{self.name}"

    @property
    def shown_category(self) -> str:
        """The category of the block `contains` finds."""
        return "command" if self.kind in ("run", "not-run") and not self.command else "output"


def run(page: str, name: str, contains: str, **kw: Any) -> Sample:
    return Sample(page, name, contains, "run", **kw)


def output(page: str, name: str, contains: str, command: str, **kw: Any) -> Sample:
    return Sample(page, name, contains, "output", command=command, **kw)


def not_run(page: str, name: str, contains: str, why: str) -> Sample:
    return Sample(page, name, contains, "not-run", why=why)


def illustrative(page: str, name: str, contains: str, why: str) -> Sample:
    return Sample(page, name, contains, "illustrative", why=why)


def on(platform: str, *strings: str) -> tuple[str, ...]:
    """The strings that only this platform prints."""
    return strings if sys.platform.startswith(platform) else ()


def count(out: str, text: str, n: int) -> None:
    assert out.count(text) == n, f"{out.count(text)} x {text!r}, not {n}:\n{out}"


# --- finding the blocks of a sample -----------------------------------------------------------


def matching(sample: Sample, blocks: list[Block] = BLOCKS) -> list[Block]:
    start = sample.contains.startswith("^")
    text = sample.contains.removeprefix("^")
    return [
        b
        for b in blocks
        if b.page == sample.page
        and b.category == sample.shown_category
        and (b.text.startswith(text) if start else text in b.text)
    ]


def shown_block(sample: Sample, blocks: list[Block] = BLOCKS) -> Block | None:
    """The output block that shows what a `run` or `output` sample prints."""
    if sample.kind == "output":
        return matching(sample, blocks)[0]
    if sample.shows is None:
        return None
    here = matching(sample, blocks)[0]
    if sample.shows == AUTO:
        following = blocks[blocks.index(here) + 1 :][:1]
        return following[0] if following and following[0].category == "output" else None
    shown = [
        b
        for b in blocks
        if b.page == sample.page and b.category == "output" and sample.shows in b.text
    ]
    return shown[0] if len(shown) == 1 else None


# --- running a sample -------------------------------------------------------------------------


def skip_reason(sample: Sample) -> str | None:
    if sample.only and not sys.platform.startswith(sample.only):
        return f"only on {sample.only}"
    for tool in sample.needs:
        if shutil.which(tool) is None:
            return f"needs {tool}"
    for element in sample.elements:
        found = subprocess.run(["gst-inspect-1.0", "--exists", element], capture_output=True)
        if found.returncode != 0:
            return f"needs the GStreamer element {element}"
    return None


# Debug output and tracers inherited from the shell would change what the commands print.
SCRUBBED = ("GST_DEBUG", "GST_DEBUG_FILE", "GST_DEBUG_DUMP_DOT_DIR", "GST_TRACERS", "G_DEBUG")


def run_command(command: str, tmp_path: Path, timeout: int) -> tuple[str, int, bool]:
    """Run a command of the docs: its output, its status, and whether it timed out."""
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
    # `python3`, `mixtape` and `pytest` in the docs are the venv's, which has mixtape
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env["PATH"]])
    env["LC_ALL"] = "C"
    env["NO_COLOR"] = "1"
    proc = subprocess.Popen(
        ["bash", "-c", command.replace("/tmp/", f"{tmp_path}/")],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=env,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        timed_out = True
    status = proc.returncode
    return out, 128 - status if status < 0 else status, timed_out


def expected_statuses(sample: Sample) -> tuple[int, ...]:
    status = sample.status
    if isinstance(status, dict):
        status = status[sys.platform]
    return status if isinstance(status, tuple) else (status,)
