"""The `mixtape` command line."""

import argparse
import asyncio
import importlib
import importlib.metadata
import json
import logging
import os
import re
import signal
import sys
from collections.abc import Callable, Sequence
from typing import Any

from .exceptions import (
    ModelError,
    PlayerDescriptionError,
    PlayerPipelineError,
    PlayerSetStateError,
)
from .model import CommandSpec, PropSpec
from .player import Player

logger = logging.getLogger("mixtape")

_log_handler: logging.Handler | None = None

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mixtape", description="Run GStreamer pipelines.")
    parser.add_argument(
        "--version", action="version", version=f"mixtape {importlib.metadata.version('mixtape')}"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="log more: -v for info, -vv for debug",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = commands.add_parser(
        "run",
        help="run a pipeline until EOS or an error, like gst-launch-1.0 -e",
        description="Run a gst-launch style pipeline description until EOS or an error.",
    )
    run.add_argument(  # also after the command; SUPPRESS keeps a count given before it
        "-v", "--verbose", action="count", default=argparse.SUPPRESS, help="log more"
    )
    _add_target(run)
    run.add_argument(
        "--set",
        dest="assignments",
        action="append",
        default=[],
        type=_assignment,
        metavar="PATH=VALUE",
        help="set a property before playing, e.g. enc.bitrate=2000; repeatable",
    )
    run.set_defaults(handler=_run)

    inspect = commands.add_parser(
        "inspect",
        help="show the commands, properties and elements a player exposes",
        description="Show what a player exposes: its commands, properties and the properties "
        "of its elements, with their types, ranges and current values.",
    )
    inspect.add_argument(
        "-v", "--verbose", action="count", default=argparse.SUPPRESS, help="log more"
    )
    _add_target(inspect)
    inspect.add_argument("--json", action="store_true", help="print the model as JSON")
    inspect.set_defaults(handler=_inspect)
    return parser


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "description",
        nargs="+",
        metavar="TARGET",
        help="a pipeline description, or module:Class for a Player subclass with a description",
    )
    parser.add_argument(
        "--expose-all",
        action="store_true",
        help="expose every element, not only the named ones",
    )


def _assignment(text: str) -> tuple[str, str]:
    path, sep, value = text.partition("=")
    if not sep or not path:
        raise argparse.ArgumentTypeError(f"{text!r} is not PATH=VALUE")
    return path, value


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits for usage errors and --version
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    _setup_logging(args.verbose)
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


def _setup_logging(verbose: int) -> None:
    level = (logging.WARNING, logging.INFO, logging.DEBUG)[min(verbose, 2)]
    global _log_handler
    if _log_handler is not None:
        logger.removeHandler(_log_handler)  # main() may run more than once in a process
    _log_handler = logging.StreamHandler(sys.stderr)
    _log_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    if not verbose:  # _play prints the pipeline error itself, without the debug string
        _log_handler.addFilter(
            lambda record: not (record.name == "mixtape.player" and record.levelno == logging.ERROR)
        )
    logger.addHandler(_log_handler)
    logger.setLevel(level)


_IMPORT_PATH = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_][\w.]*")


def _load_player(args: argparse.Namespace) -> Player | None:
    """Build the player of the target: a description, or `module:Class` and its own.
    Prints the error and returns None when that fails."""
    target = " ".join(args.description)
    try:
        if _IMPORT_PATH.fullmatch(target) is None:
            return Player.from_description(target, expose_all=args.expose_all)
        cls = _import_player_class(target)
        if cls.description is None:
            raise ValueError(f"{target} has no description to build a player from")
        return cls.from_description(cls.description, expose_all=args.expose_all)
    except (ValueError, PlayerDescriptionError, ModelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _import_player_class(target: str) -> type[Player]:
    """Import `module:Class` (dotted attributes allowed); a ValueError says what is wrong."""
    module_name, _, attribute = target.partition(":")
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())  # like uvicorn, to import what is next to the user
    try:
        value: Any = importlib.import_module(module_name)
    except Exception as exc:  # whatever the module does when it is imported
        raise ValueError(f"cannot import {module_name!r}: {exc}") from exc
    for part in attribute.split("."):
        try:
            value = getattr(value, part)
        except AttributeError:
            raise ValueError(f"{module_name!r} has no attribute {attribute!r}") from None
    if not (isinstance(value, type) and issubclass(value, Player)):
        raise ValueError(f"{target} is not a Player subclass")
    return value


def _run(args: argparse.Namespace) -> int:
    player = _load_player(args)
    if player is None:
        return EXIT_USAGE
    return asyncio.run(_play(player, args.verbose, args.assignments))


# -- inspect -- #


def _inspect(args: argparse.Namespace) -> int:
    player = _load_player(args)
    if player is None:
        return EXIT_USAGE
    spec = player.describe()
    if args.json:
        print(spec.model_dump_json(indent=2))
        return 0
    print(spec.name)
    if spec.commands:
        print("\nCommands:")
        print(_table([_command_row(c) for c in spec.commands.values()], cap=64))
    print("\nProperties:")
    print(_table([_prop_row(player, p, p.path) for p in spec.properties.values()]))
    for element in spec.elements.values():
        long_name = f" — {element.description}" if element.description else ""
        print(f"\n{element.name} ({element.factory}){long_name}")
        rows = [_prop_row(player, p, p.property or p.path) for p in element.properties.values()]
        print(_table(rows))
    return 0


def _table(rows: list[list[str]], cap: int = 32) -> str:
    """Rows of cells as lines indented by two spaces, all columns but the last aligned.
    A cell wider than `cap` (an enum of 25 nicks) overflows instead of widening its
    column for every row."""
    widths = [
        max((len(c) for c in col if len(c) <= cap), default=0) for col in zip(*rows, strict=True)
    ]
    lines = (
        "  ".join([*(c.ljust(w) for c, w in zip(row[:-1], widths, strict=False)), row[-1]])
        for row in rows
    )
    return "\n".join(f"  {line}".rstrip() for line in lines)


def _command_row(command: CommandSpec) -> list[str]:
    required = set(command.parameters.get("required", ()))
    params = []
    for name, schema in command.parameters.get("properties", {}).items():
        text = f"{name}: {_type(schema, keep_null=True)}"
        if name not in required:
            text += f" = {_value(schema.get('default'))}"
        params.append(text)
    return [f"{command.name}({', '.join(params)})", command.description or ""]


def _prop_row(player: Player, prop: PropSpec, name: str) -> list[str]:
    access = ("r" if prop.readable else "-") + ("w" if prop.writable else "-")
    value = f"= {_value(player.get(prop.path))}" if prop.readable else ""
    notes = []
    if prop.target:
        notes.append(f"→ {prop.target}")
    if prop.mutable:
        notes.append(f"mutable: {prop.mutable}")
    return [name, access, _type(prop.value_schema), value, "  ".join(notes), prop.description or ""]


def _type(schema: dict[str, Any], *, keep_null: bool = False) -> str:
    """A compact type from a JSON Schema: `integer 1..8`, `a|b|c`, `flags a|b`."""
    if str(schema.get("format", "")).startswith("gst-"):
        return str(schema["format"]).removeprefix("gst-")  # caps, structure
    if "anyOf" in schema:
        options = [o for o in schema["anyOf"] if keep_null or o.get("type") != "null"]
        return " | ".join(_type(o, keep_null=keep_null) for o in options)
    if "enum" in schema:
        return "|".join(map(str, schema["enum"]))
    if "const" in schema:
        return str(schema["const"])
    kind = str(schema.get("type", "any"))
    if kind == "array" and "items" in schema:
        return f"flags {_type(schema['items'])}"
    if "minimum" in schema and "maximum" in schema:
        low, high = schema["minimum"], schema["maximum"]
        if kind == "integer":
            return f"{kind} {low}..{high}"
        if kind == "number" and max(abs(low), abs(high)) < 1e9:  # not float's own limits
            return f"{kind} {low:g}..{high:g}"
    return kind


def _value(value: Any) -> str:
    if isinstance(value, list):
        return "+".join(value) or "none"
    if isinstance(value, str):
        return value or '""'
    if isinstance(value, float):
        return f"{value:.7g}"  # a float32 property reads back as 1.399999976158142
    return json.dumps(value)


async def _play(player: Player, verbose: int, assignments: Sequence[tuple[str, str]] = ()) -> int:
    """Play until EOS, stopping with an EOS on the first SIGINT or SIGTERM and at once
    on the second. Returns the exit code."""
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    interrupts = 0
    stopping: asyncio.Task[None] | None = None

    def interrupt() -> None:
        nonlocal interrupts, stopping
        interrupts += 1
        if interrupts == 1:
            print("Interrupt: stopping (EOS)…", file=sys.stderr)
            stopping = loop.create_task(player.stop(eos=True))
        else:
            # GStreamer can block in the teardown that follows (as it does in gst-launch):
            # a third Ctrl-C then kills the process, as the default handler does.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            if main_task is not None:
                main_task.cancel()

    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, interrupt)
    pump: asyncio.Task[None] | None = None
    try:
        if sys.platform == "darwin":
            from . import macos

            pump = macos.start_pump()  # before the pipeline leaves NULL
        async with player:
            for path, value in assignments:
                await player.set(path, value)
            await player.play_until_eos()
    except asyncio.CancelledError:
        if interrupts < 2:
            raise
        return EXIT_INTERRUPTED
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except PlayerPipelineError as exc:
        print(f"error: {exc.source}: {exc.message}", file=sys.stderr)
        if verbose and exc.debug:
            print(exc.debug, file=sys.stderr)
        return EXIT_ERROR
    except PlayerSetStateError as exc:
        if interrupts:  # the stop could not finish an EOS, but it was asked for
            return 0
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        for task in (pump, stopping):
            if task is not None:
                task.cancel()
    return 0
