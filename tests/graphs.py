"""The pipeline graphs of the pages: Mermaid flowcharts made from mixtape's own graph of a
pipeline (`Player.graph()`, which the control page draws too).

A graph on a page is a ```mermaid block whose second line is `%% <sample name>`; its sample in
tests/samples/ gives the pipeline descriptions it shows, and tests/test_doc_samples.py checks
that the block is what this module makes of them. To write or update the blocks of the pages:

    python -m tests.graphs
"""

import ast
import asyncio
import importlib.util
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from mixtape import Player  # noqa: E402
from mixtape.spec import GraphSpec  # noqa: E402
from tests.examples import EXAMPLES  # noqa: E402

# "<pipeline index>.<element>" of both ends, the element by its name or its factory, and a label
Link = tuple[str, str, str]


async def _graph(description: str) -> tuple[GraphSpec, dict[str, str]]:
    """The graph of a description, and the caps of its capsfilters by key."""
    async with Player.from_description(description) as player:
        spec = player.graph()
        caps = {}
        for key, node in spec.nodes.items():
            if node.factory == "capsfilter":
                element = player.pipeline.get_by_name(node.name)
                caps[key] = element.get_property("caps").to_string()
        return spec, caps


# Mermaid's default spacing makes a pipeline of eight elements wider than the page, which then
# shrinks its text
SPACING = (
    '%%{init: {"flowchart": {"nodeSpacing": 20, "rankSpacing": 24,'
    ' "subGraphTitleMargin": {"top": 4, "bottom": 12}}}}%%'
)


def module(example: str) -> Any:
    """A program of examples/ that only runs under `__main__`, imported for what it defines."""
    spec = importlib.util.spec_from_file_location(Path(example).stem, EXAMPLES / example)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def literal(example: str, name: str) -> str:
    """The string a program of examples/ assigns to `name`, so that its graph follows it."""
    tree = ast.parse((EXAMPLES / example).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, str)
            return value
    raise AssertionError(f"examples/{example} assigns no string to {name}")


def _auto(name: str, factory: str) -> bool:
    """Whether GStreamer named the element after its factory: its number depends on what ran
    before in the process, so the graph leaves it out."""
    return re.fullmatch(rf"{re.escape(factory)}\d+", name) is not None


def _label(key: str, spec: GraphSpec, caps: dict[str, str]) -> str:
    node = spec.nodes[key]
    if key in caps:  # the caps say more than capsfilter0
        fields = re.sub(r"\(\w+\)|\s", "", caps[key]).replace('"', "#quot;").split(",")
        return "<br>".join(fields)
    if _auto(node.name, node.factory):
        return node.factory
    return f"<b>{node.name}</b><br>{node.factory}"


def _shown(key: str, spec: GraphSpec) -> str:
    """The node that shows an element: itself, or the outermost element bin it is in. Only
    plain bins (`( … )` in a description) are drawn open; decodebin or webrtcsink are one node."""
    shown, parent = key, spec.nodes[key].parent
    while parent is not None:
        if spec.nodes[parent].factory != "bin":
            shown = parent
        parent = spec.nodes[parent].parent
    return shown


def mermaid(
    name: str,
    pipelines: Sequence[tuple[str, str]],
    links: Sequence[Link] = (),
    direction: str = "LR",
) -> str:
    """The block of a graph: one or more pipelines, as (title, description), and the links
    between them or made only while playing (a decodebin's), drawn dashed. A graph too wide
    for the page goes top to bottom (`TB`)."""
    lines = [SPACING, f"flowchart {direction}", f"  %% {name}"]
    edges = []
    ends: dict[str, str] = {}  # "<pipeline>.<name or factory>" -> its node's id
    for i, (title, description) in enumerate(pipelines):
        spec, caps = asyncio.run(_graph(description))
        indent = "  "
        if len(pipelines) > 1:
            lines.append(f'  subgraph p{i}["{title}"]')
            indent = "    "
        shown = {key: _shown(key, spec) for key in spec.nodes}
        ids = {key: f"p{i}n{n}" for n, key in enumerate(spec.nodes)}
        for key, node in reversed(spec.nodes.items()):
            ends[f"{i}.{node.factory}"] = ends[f"{i}.{node.name}"] = ids[shown[key]]
        open_bins: list[str] = []
        for key, node in spec.nodes.items():
            if shown[key] != key:
                continue
            while open_bins and spec.nodes[key].parent != open_bins[-1]:
                open_bins.pop()
                lines.append(f"{indent}{'  ' * len(open_bins)}end")
            pad = indent + "  " * len(open_bins)
            if node.bin and node.factory == "bin":
                title = "bin" if _auto(node.name, "bin") else node.name
                lines.append(f'{pad}subgraph {ids[key]}["{title}"]')
                open_bins.append(key)
            else:
                lines.append(f'{pad}{ids[key]}["{_label(key, spec, caps)}"]')
        for depth in range(len(open_bins) - 1, -1, -1):
            lines.append(f"{indent}{'  ' * depth}end")
        if len(pipelines) > 1:
            lines.append("  end")
        for link in spec.links:
            edge = f"  {ids[shown[link.source]]} --> {ids[shown[link.sink]]}"
            if shown[link.source] != shown[link.sink] and edge not in edges:
                edges.append(edge)
    for source, sink, label in links:
        edges.append(f'  {ends[source]} -. "{label}" .-> {ends[sink]}')
    return "\n".join(lines + edges)


def main() -> None:
    """Write the graph blocks of the pages from their samples."""
    from tests.doc_samples import PAGES, matching
    from tests.samples import SAMPLES

    Gst.init(None)
    texts = {page.name: page.read_text() for page in PAGES}
    for sample in SAMPLES:
        if sample.kind != "graph":
            continue
        (block,) = matching(sample)
        new = mermaid(sample.name, sample.pipelines, sample.links, sample.direction)
        texts[block.page] = texts[block.page].replace(block.text, new, 1)
        print(f"{block.where}: {sample.id}" + (" (unchanged)" if new == block.text else ""))
    for page in PAGES:
        page.write_text(texts[page.name])


if __name__ == "__main__":
    main()
