"""The `mixtape` command line."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import importlib
import importlib.metadata
import json
import logging
import os
import re
import signal
import sys
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .perf import compare as perf_compare

# PyGObject and GStreamer come from the system and may be missing: `--help`, `--version` and
# `perf compare` work without them, so only the commands that need them import them.
if TYPE_CHECKING:
    from .events import EventStream
    from .model import CommandSpec, PropSpec
    from .player import Player

logger = logging.getLogger("mixtape")
_uvicorn_logger = logging.getLogger("uvicorn")

_log_handler: logging.Handler | None = None

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
HTTP_HOST = "127.0.0.1"
NO_GST = (
    "error: mixtape needs PyGObject and GStreamer 1.0 from the system Python: "
    "see https://shashi.to/mixtape/troubleshooting/"
)
VERBOSE_HELP = "log more: -v info, -vv debug"


class _Formatter(argparse.HelpFormatter):
    """Wraps each line of a description or epilog on its own, so that an epilog can list
    examples, one to a line."""

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        lines = []
        for line in text.split("\n"):
            deeper = indent + line[: len(line) - len(line.lstrip())]
            lines.append(super()._fill_text(line, width, deeper))
        return "\n".join(lines)


def _command(commands: Any, name: str, **kwargs: Any) -> argparse.ArgumentParser:
    return commands.add_parser(name, allow_abbrev=False, formatter_class=_Formatter, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mixtape",
        description="Run GStreamer pipelines.",
        allow_abbrev=False,
        formatter_class=_Formatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"mixtape {importlib.metadata.version('mixtape')}"
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help=VERBOSE_HELP)
    parser.set_defaults(needs_gst=True)
    # `-v` of a command adds to the `-v` before it: it has a count of its own
    verbose = argparse.ArgumentParser(add_help=False)
    verbose.add_argument(
        "-v", "--verbose", dest="more_verbose", action="count", default=0, help=VERBOSE_HELP
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = _command(
        commands,
        "run",
        parents=[verbose],
        help="run a pipeline until EOS or an error, like gst-launch-1.0 -e",
        description="Run a gst-launch style pipeline description until EOS or an error, "
        "or with --http until interrupted.",
        epilog="Examples:\n"
        '  mixtape run "videotestsrc ! autovideosink"\n'
        "  mixtape run --http 8047 demo:Demo\n"
        "\n"
        "Exit status: 0 at EOS or after a first Ctrl-C, 1 on a pipeline error or when --http "
        "cannot listen, 2 on bad usage, 130 on a second Ctrl-C.",
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
    run.add_argument(
        "--http",
        type=_http_address,
        metavar="[HOST:]PORT",
        help="control the player from a browser (needs the http extra) and keep running "
        "after EOS or an error until interrupted; HOST defaults to 127.0.0.1",
    )
    run.set_defaults(handler=_run)

    inspect = _command(
        commands,
        "inspect",
        parents=[verbose],
        help="show the commands, properties and elements a player exposes",
        description="Show what a player exposes: its commands, properties and the properties "
        "of its elements, with their types, ranges and current values.",
        epilog="Exit status: 0 on success, 2 on bad usage or a target that cannot be loaded, "
        "130 on Ctrl-C.",
    )
    _add_target(inspect)
    inspect.add_argument("--json", action="store_true", help="print the model as JSON")
    inspect.set_defaults(handler=_inspect)

    perf = _command(
        commands,
        "perf",
        help="work with what the perf feature measured",
        description="Work with the folders the perf feature and `pytest --mixtape-perf` write.",
    )
    perf_commands = perf.add_subparsers(dest="perf_command", required=True, metavar="COMMAND")
    compare = _command(
        perf_commands,
        "compare",
        parents=[verbose],
        help="find what got slower between two sessions or two runs",
        description="Compare two sessions of `pytest --mixtape-perf` (folders with an "
        "index.json) or two single runs (folders with a summary.json, as `mixtape run "
        "--feature perf,out=DIR` writes): the p95 and max of every metric where higher is "
        "worse. A value is a regression when it grew by more than the threshold and by more "
        "than a floor of its unit (100 µs, 5 points of CPU, 5% of a queue). Exits 1 on a "
        "regression.",
        epilog="Exit status: 0 when nothing got slower, 1 on a regression, 2 on a folder that "
        "cannot be compared or bad usage, 130 on Ctrl-C.",
    )
    compare.add_argument("base", type=Path, metavar="BASE", help="the folder to compare with")
    compare.add_argument("new", type=Path, metavar="NEW", help="the folder to check")
    compare.add_argument(
        "--threshold",
        type=_percentage,
        default=perf_compare.THRESHOLD * 100,
        metavar="PERCENT",
        help="how much a value may grow over the base, in percent (default: %(default).0f)",
    )
    compare.add_argument("--json", action="store_true", help="print the comparison as JSON")
    compare.set_defaults(handler=_perf_compare, needs_gst=False)
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
    installed = ", ".join(sorted(_installed_features())) or "none installed"
    parser.add_argument(
        "--feature",
        action="append",
        default=[],
        metavar="NAME[,KEY=VALUE…]",
        help="enable a feature with its options, e.g. leaks,interval=1; repeatable; a value "
        "has commas only inside parentheses. "
        f"Available: {installed}, or gst:NAME for any GStreamer tracer",
    )


def _installed_features() -> Iterable[str]:
    try:
        from . import features
    except (ImportError, ValueError):  # no GStreamer: `--help` still lists the names
        return [ep.name for ep in importlib.metadata.entry_points(group="mixtape.features")]
    return features.available()


def _assignment(text: str) -> tuple[str, str]:
    path, sep, value = text.partition("=")
    if not sep or not path:
        raise argparse.ArgumentTypeError(f"{text!r} is not PATH=VALUE")
    return path, value


def _http_address(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not port.isascii() or not port.isdecimal() or int(port) > 65535:
        raise argparse.ArgumentTypeError(f"{text!r} is not [HOST:]PORT")
    return host.strip("[]") or HTTP_HOST, int(port)


def _percentage(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not value >= 0:  # also nan
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of percent, 0 or more")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits for usage errors and --version
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    args.verbose += args.more_verbose
    _setup_logging(args.verbose)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        if args.needs_gst and not _gst_available():
            print(NO_GST, file=sys.stderr)
            return EXIT_ERROR
        return handler(args)
    except KeyboardInterrupt:  # outside the play loop, which handles Ctrl-C itself
        return EXIT_INTERRUPTED


def _gst_available() -> bool:
    try:
        importlib.import_module("mixtape.player")  # imports gi and GStreamer
    except (ImportError, ValueError):  # gi.require_version raises ValueError
        return False
    return True


_CAPITALISED = re.compile(r"[A-Z](?=[a-z]+\b)")


def _error(message: object) -> None:
    """Print `error: message`, its first word in lower case if it is a capitalised word (the
    texts of the library are sentences; a name such as `Player` stays)."""
    text = str(message)
    if _CAPITALISED.match(text):
        text = text[0].lower() + text[1:]
    print(f"error: {text}", file=sys.stderr)


def _setup_logging(verbose: int) -> None:
    level = (logging.WARNING, logging.INFO, logging.DEBUG)[min(verbose, 2)]
    global _log_handler
    if _log_handler is not None:  # main() may run more than once in a process
        for named in (logger, _uvicorn_logger):
            named.removeHandler(_log_handler)
    _log_handler = logging.StreamHandler(sys.stderr)
    _log_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    if not verbose:  # _play prints the pipeline error itself, without the debug string
        _log_handler.addFilter(
            lambda record: not (record.name == "mixtape.player" and record.levelno == logging.ERROR)
        )
    logger.addHandler(_log_handler)
    logger.setLevel(level)
    _uvicorn_logger.addHandler(_log_handler)  # the http extra's server, which `-v` also raises


_IMPORT_PATH = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_][\w.]*")


def _load_player(args: argparse.Namespace) -> Player | None:
    """Build the player of the target: a description, or `module:Class` and its own.
    Prints the error and returns None when that fails."""
    from .exceptions import ModelError, PlayerDescriptionError
    from .player import Player

    target = " ".join(args.description)
    try:
        if _IMPORT_PATH.fullmatch(target) is None:
            return Player.from_description(
                target, expose_all=args.expose_all, features=args.feature
            )
        cls = _import_player_class(target)
        if cls.description is None:
            raise ValueError(f"{target} has no description to build a player from")
        return cls.from_description(
            cls.description, expose_all=args.expose_all, features=args.feature
        )
    except (ValueError, PlayerDescriptionError, ModelError) as exc:
        _error(exc)
        return None


def _import_player_class(target: str) -> type[Player]:
    """Import `module:Class` (dotted attributes allowed); a ValueError says what is wrong."""
    from .player import Player

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
    if args.http is not None:
        try:
            importlib.import_module("mixtape.interfaces.http")  # needs the extra
        except ImportError:
            _error("--http needs the http extra: pip install 'mixtape[http]'")
            return EXIT_USAGE
    player = _load_player(args)
    if player is None:
        return EXIT_USAGE
    return asyncio.run(_play(player, args.verbose, args.assignments, args.http))


# -- perf -- #


def _perf_compare(args: argparse.Namespace) -> int:
    try:
        result = perf_compare.compare_folders(args.base, args.new, args.threshold / 100)
    except perf_compare.CompareError as exc:
        _error(exc)
        return EXIT_USAGE
    print(json.dumps(result, indent=2) if args.json else perf_compare.format_text(result))
    return EXIT_ERROR if result["regressions"] else 0


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
    if player.enabled_features:
        print(f"Features: {', '.join(player.enabled_features)}")
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
    """A compact type from a JSON Schema: `integer 1..8`, `a|b|c`, `flags a|b`, `list string`."""
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
        # only a choice of values makes flags, as on the page; other arrays are lists
        return f"{'flags' if 'enum' in schema['items'] else 'list'} {_type(schema['items'])}"
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


async def _play(
    player: Player,
    verbose: int,
    assignments: Sequence[tuple[str, str]] = (),
    http: tuple[str, int] | None = None,
) -> int:
    """Play until EOS, stopping with an EOS on the first SIGINT or SIGTERM and at once
    on the second. Returns the exit code.

    With `http` (host, port) it serves the player instead, and neither an EOS nor an
    error ends it: only the first interrupt does, once the player has stopped."""
    from gi.repository import Gst

    from .exceptions import (
        ModelError,
        PlayerPipelineError,
        PlayerSetStateError,
        UnknownNameError,
    )

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    interrupts = 0
    stopping: asyncio.Task[None] | None = None
    interrupted = asyncio.Event()

    def interrupt() -> None:
        nonlocal interrupts, stopping
        interrupts += 1
        if interrupts == 1:
            print("Interrupt: stopping (EOS)…", file=sys.stderr)
            stopping = loop.create_task(player.stop(eos=True))
            interrupted.set()
        elif interrupts == 2:
            # The teardown that follows waits for a hung pipeline for up to its
            # `state_timeout`: a third Ctrl-C then kills the process, as the default does.
            print("Interrupt: stopping now (Ctrl-C again to kill)…", file=sys.stderr)
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
            if http is None:
                await player.play_until_eos()
            else:
                async with _serving(player, *http, verbose, interrupted):
                    await interrupted.wait()
                    assert stopping is not None
                    await stopping
    except asyncio.CancelledError:
        if interrupts < 2:
            raise
        return EXIT_INTERRUPTED
    except ModelError as exc:
        if isinstance(exc, UnknownNameError) and str(exc).startswith("Unknown property"):
            exc = UnknownNameError(f"{exc}: mixtape inspect TARGET lists them")
        _error(exc)
        return EXIT_USAGE
    except PlayerPipelineError as exc:
        print(f"error: {exc.source}: {exc.message}", file=sys.stderr)
        if verbose and exc.debug:
            print(exc.debug, file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:  # --http cannot listen
        if http is None:
            raise
        if exc.errno == errno.EADDRINUSE:
            _error(
                f"{http[0]}:{http[1]} is in use (is another mixtape run --http running?): "
                "choose another port with --http PORT"
            )
        else:
            _error(exc)
        return EXIT_ERROR
    except PlayerSetStateError as exc:
        if interrupts and player.state == Gst.State.NULL:  # the stop ended a wait, as asked
            return 0
        _error(exc)
        return EXIT_ERROR
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        for task in (pump, stopping):
            if task is not None:
                task.cancel()
        if stopping is not None and stopping.done() and not stopping.cancelled():
            stopping.exception()  # the teardown raised it again, or it did not matter
    return 0


@contextlib.asynccontextmanager
async def _serving(
    player: Player, host: str, port: int, verbose: int, stopping: asyncio.Event
) -> AsyncIterator[None]:
    """Serve the player over HTTP and play it, until the block ends. Errors of the pipeline
    are printed and leave the server up, so a page can play again. `stopping` is set once
    the user interrupts."""
    from .exceptions import PlayerPipelineError, PlayerSetStateError
    from .interfaces.http import serve  # needs the http extra, which `_run` has checked
    from .interfaces.http.security import is_loopback

    if not is_loopback(host):
        print(
            f"warning: serving on {host} without authentication: anyone who can reach it "
            "can control the player",
            file=sys.stderr,
        )
    log_level = ("warning", "info", "debug")[min(verbose, 2)]
    async with serve(player, host, port, log_level=log_level) as listening:
        print(f"Serving on {listening.url}", file=sys.stderr)
        stream = player.events()
        reporter = asyncio.create_task(_report_errors(stream, verbose))
        try:
            try:
                await player.play()
            except PlayerPipelineError:
                pass  # the reporter prints it
            except PlayerSetStateError as exc:
                if not stopping.is_set():  # a Ctrl-C during the preroll interrupts it
                    _error(exc)
            yield
        finally:
            stream.close()
            reporter.cancel()


async def _report_errors(stream: EventStream, verbose: int) -> None:
    """Print the pipeline errors."""
    from .events import Error

    async for event in stream:
        if isinstance(event, Error):
            print(f"error: {event.error.source}: {event.error.message}", file=sys.stderr)
            if verbose and event.error.debug:
                print(event.error.debug, file=sys.stderr)
