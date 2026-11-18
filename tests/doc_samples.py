"""The machinery of tests/test_doc_samples.py: the fenced blocks of the pages and the samples
that test them. The samples themselves are in tests/samples/, one module per page.

A command is shown with its output, as click's documentation does: a ```console block is its
`$ ` lines (with their `\\` continuations), run as one command, then what they print. The
tests read it as two blocks, the command and the output. Every fenced block in README.md and
docs/*.md that is not an example program (`--8<--`) is a command, an output or a graph, and
every one needs a sample:

- `run`: a command, run verbatim with `bash -c` in the repository. Its status and its expected
  strings are checked, and every expected string must also be in the output under it (or in
  the block `shows` names), so the output the page shows cannot drift from what it prints.
- `output`: an output whose command is not the one shown: `command` produces it.
- `prints`: a program of examples/ whose output is on the page as pydantic's documentation
  shows it, in `# >` comments under its prints (ruff format writes `#>` as `# >`). It must
  print those lines, in order, and the page must include the sections that have them.
- `not_run`: a command that is setup, needs a camera, or runs until Ctrl-C, with the reason.
- `graph`: a ```mermaid graph of pipelines, which must be what tests/graphs.py makes of
  their descriptions (`python -m tests.graphs` writes it).

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
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
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
        """`command`, `output`, `graph`, or `example` (a program included from examples/)."""
        if "--8<--" in self.text:
            return "example"
        if self.lang == "mermaid":
            return "graph"
        if self.lang == "console":
            return "command"
        if self.lang in ("", "text"):
            return "output"
        assert self.lang not in ("sh", "bash", "shell"), (
            f"{self.where}: write a command as ```console, `$ ` and its output"
        )
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
        if lang == "console":
            blocks.extend(console(page, start, title[1] if title else "", body))
        else:
            blocks.append(Block(page, start, lang, title[1] if title else "", "\n".join(body)))
    return blocks


def console(page: str, start: int, title: str, body: list[str]) -> list[Block]:
    """A ```console block is a command, its `$ ` lines and their `\\` continuations, and the
    output that follows them: the two blocks of a sample."""
    command: list[str] = []
    n = 0
    while n < len(body) and (body[n].startswith("$ ") or (command and command[-1].endswith("\\"))):
        command.append(body[n].removeprefix("$ "))
        n += 1
    assert command, f"{page}:{start}: a console block starts with a `$ ` command"
    blocks = [Block(page, start, "console", "", "\n".join(command))]
    if n < len(body):
        assert not any(line.startswith("$ ") for line in body[n:]), (
            f"{page}:{start}: the `$ ` lines of a console block come first, then their output"
        )
        blocks.append(Block(page, start + n + 1, "text", title, "\n".join(body[n:])))
    return blocks


BLOCKS = [b for path in PAGES for b in extract(path)]


# --- the samples ------------------------------------------------------------------------------

Status = int | tuple[int, ...] | dict[str, int]


@dataclass
class Sample:
    page: str
    name: str
    contains: str  # a unique substring of the block; `^` starts it with it
    kind: str  # run, output, prints, not-run, graph
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
    pipelines: tuple[tuple[str, str], ...] = ()  # graph: (title, description) of each pipeline
    links: tuple[tuple[str, str, str], ...] = ()  # graph: dashed links, see tests/graphs.py
    direction: str = "LR"  # graph: or TB

    @property
    def id(self) -> str:
        return f"{self.page}:{'graph:' if self.kind == 'graph' else ''}{self.name}"

    @property
    def shown_category(self) -> str:
        """The category of the block `contains` finds."""
        if self.kind == "graph":
            return "graph"
        return "command" if self.kind in ("run", "not-run") and not self.command else "output"


def run(page: str, name: str, contains: str, **kw: Any) -> Sample:
    return Sample(page, name, contains, "run", **kw)


def output(page: str, name: str, contains: str, command: str, **kw: Any) -> Sample:
    return Sample(page, name, contains, "output", command=command, **kw)


def prints(page: str, name: str, program: str, **kw: Any) -> Sample:
    """A program of examples/ whose output is on the page as `# >` comments under its prints."""
    return Sample(page, name, program, "prints", command=f"python3 {program}", **kw)


def not_run(page: str, name: str, contains: str, why: str) -> Sample:
    return Sample(page, name, contains, "not-run", why=why)


def graph(page: str, name: str, *pipelines: str | tuple[str, str], **kw: Any) -> Sample:
    """The graph `%% name` of a page: of one pipeline, or of several, each with a title."""
    titled = tuple(("", p) if isinstance(p, str) else p for p in pipelines)
    return Sample(page, name, f"%% {name}\n", "graph", pipelines=titled, **kw)


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


# --- the output in a program's comments -------------------------------------------------------

INLINE = re.compile(r"^\s*# >(?: (.*))?$")  # ruff format writes pydantic's `#>` as `# >`
INCLUDED = re.compile(r'--8<-- "([^":]+):([^"]+)"')


def inline_output(program: str) -> list[str]:
    """The lines of the `# >` comments of a program, in order: what it prints."""
    lines = (ROOT / program).read_text().splitlines()
    return [m[1] or "" for line in lines if (m := INLINE.match(line))]


def shown_output(page: str, program: str, blocks: list[Block] = BLOCKS) -> list[str]:
    """The `# >` lines in the sections of a program that a page includes."""
    text = (ROOT / program).read_text()
    shown: list[str] = []
    for block in blocks:
        if block.page == page and (m := INCLUDED.search(block.text)) and m[1] == program:
            section = re.search(rf"\[start:{m[2]}\]\n(.*?)\n[^\n]*\[end:{m[2]}\]", text, re.S)
            assert section, f"{program} has no section {m[2]}"
            lines = section[1].splitlines()
            shown += [c[1] or "" for line in lines if (c := INLINE.match(line))]
    return shown


def programs_with_output(blocks: list[Block] = BLOCKS) -> set[tuple[str, str]]:
    """(page, program) for each program a page shows `# >` lines of: each needs a `prints`."""
    found: set[tuple[str, str]] = set()
    for block in blocks:
        if (m := INCLUDED.search(block.text)) and m[1].endswith(".py"):
            if shown_output(block.page, m[1], [block]):
                found.add((block.page, m[1]))
    return found


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


# The ports the pages serve on: a command gets free ones instead, and its output the page's back,
# so that a server running on the machine does not fail it. 8000 is `mkdocs serve`'s: the pages
# do not use it.
PORTS = ("8047", "8048", "8101", "8102")


def free_port() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return str(sock.getsockname()[1])


def run_command(command: str, tmp_path: Path, timeout: int) -> tuple[str, int, bool]:
    """Run a command of the docs: its output, its status, and whether it timed out."""
    ports = {port: free_port() for port in PORTS if re.search(rf"\b{port}\b", command)}
    for page, free in ports.items():
        command = re.sub(rf"\b{page}\b", free, command)
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
    # `python3`, `mixtape` and `pytest` in the docs are the venv's, which has mixtape
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env["PATH"]])
    env["LC_ALL"] = "C"
    env["NO_COLOR"] = "1"
    env["COLUMNS"] = "80"  # `--help` wraps its text to the width of the terminal
    # a `pytest` of the docs reads this project's ini: not its 20 s per test, the sample's limit
    env["PYTEST_ADDOPTS"] = f"--timeout={timeout}"
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
    for page, free in ports.items():
        out = re.sub(rf"\b{free}\b", page, out)
    status = proc.returncode
    return out, 128 - status if status < 0 else status, timed_out


def expected_statuses(sample: Sample) -> tuple[int, ...]:
    status = sample.status
    if isinstance(status, dict):
        status = status[sys.platform]
    return status if isinstance(status, tuple) else (status,)
