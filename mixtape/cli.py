"""The `mixtape` command line."""

from __future__ import annotations

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
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple

import typer
from typer._click.exceptions import ClickException, UsageError
from typer.core import TyperCommand, TyperOption
from typer.main import get_command

# PyGObject and GStreamer come from the system and may be missing, and `--help` should be quick:
# `--help` and `perf compare` work without them and without importing the perf code, so only
# what needs them imports them (`--version` reports whether they are there).
if TYPE_CHECKING:
    from typer._click.core import Context
    from typer._click.formatting import HelpFormatter

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
THRESHOLD_PERCENT = 50  # `perf.compare.THRESHOLD` as percent: a test keeps them in step
NO_GST = (
    "error: mixtape needs PyGObject and GStreamer 1.0 from the system Python: "
    "see https://shashi.to/mixtape/troubleshooting/"
)
VERBOSE_HELP = "log more: -v info, -vv debug"
TARGET_HELP = (
    "TARGET is a pipeline description, as one quoted string or as its words, or a player "
    "class that has a description of its own: module:Class (a module in the current folder "
    "or on sys.path) or path/to/file.py:Class (a file: its folder is put on sys.path). A "
    "player class takes no further words."
)
FEATURE_HELP = (
    "enable a feature with its options, e.g. leaks,interval=1; repeatable; a value has commas "
    "only inside parentheses"
)
_VERBOSE = "mixtape.verbose"  # the `-v`s before the command, in `Context.meta`


class _Command(TyperCommand):
    """A command whose usage reads `TARGET...` and whose help, when it is asked for, lists the
    installed features."""

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        pieces = super().collect_usage_pieces(ctx)
        return [piece.replace("{", "").replace("}", "") for piece in pieces]

    def format_help(self, ctx: Context, formatter: HelpFormatter) -> None:
        installed = ", ".join(sorted(_installed_features())) or "none installed"
        for param in self.params:
            if isinstance(param, TyperOption) and param.name == "feature":
                param.help = (
                    f"{FEATURE_HELP}. Available: {installed}, or gst:NAME for any GStreamer tracer"
                )
        super().format_help(ctx, formatter)


def _typer(**kwargs: Any) -> typer.Typer:
    # plain help and errors, which a terminal and a grep read alike
    return typer.Typer(
        add_completion=False,
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
        no_args_is_help=True,
        **kwargs,
    )


app = _typer(
    help="Run GStreamer pipelines.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
perf_app = _typer(help="Work with the folders the perf feature and `pytest --mixtape-perf` write.")
app.add_typer(perf_app, name="perf", short_help="work with what the perf feature measured")

Verbose = Annotated[
    int, typer.Option("-v", "--verbose", count=True, show_default=False, help=VERBOSE_HELP)
]
Target = Annotated[list[str], typer.Argument(metavar="TARGET", hidden=True)]
ExposeAll = Annotated[
    bool, typer.Option("--expose-all", help="expose every element, not only the named ones")
]
Features = Annotated[
    list[str] | None,
    typer.Option("--feature", metavar="NAME[,KEY=VALUE…]", show_default=False, help=FEATURE_HELP),
]
AsJson = Annotated[bool, typer.Option("--json", help="print the model as JSON")]


def _installed_features() -> Iterable[str]:
    """The names of the installed features, as `features.available()`, without importing them."""
    return [ep.name for ep in importlib.metadata.entry_points(group="mixtape.features")]


class HttpAddress(NamedTuple):
    host: str
    port: int


def _assignment(text: str) -> str:
    path, sep, _ = text.partition("=")
    if not sep or not path:
        raise typer.BadParameter(f"{text!r} is not PATH=VALUE")
    return text


def _http_address(text: str) -> HttpAddress:
    host, _, port = text.rpartition(":")
    if not port.isascii() or not port.isdecimal() or int(port) > 65535:
        raise typer.BadParameter(f"{text!r} is not [HOST:]PORT")
    return HttpAddress(host.strip("[]") or HTTP_HOST, int(port))


def _percentage(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not value >= 0:  # also nan
        raise typer.BadParameter(f"{text!r} is not a number of percent, 0 or more")
    return value


def _version_lines() -> list[str]:
    """What a bug report needs: the versions of mixtape, GStreamer, PyGObject and Python."""
    try:
        import gi
    except ImportError:
        gstreamer = pygobject = "not found"
    else:
        pygobject = ".".join(map(str, gi.version_info))
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError):  # require_version raises ValueError
            gstreamer = "not found"
        else:
            Gst.init(None)  # `Gst.version` needs it; the process exits next
            major, minor, micro, nano = Gst.version()
            gstreamer = f"{major}.{minor}.{micro}" + (f".{nano}" if nano else "")
    return [
        f"mixtape {importlib.metadata.version('mixtape')}",
        f"gstreamer: {gstreamer}",
        f"pygobject: {pygobject}",
        f"python: {sys.version.split()[0]}",
    ]


def _show_version(value: bool) -> None:
    if value:
        print("\n".join(_version_lines()))
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    verbose: Verbose = 0,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="show the versions of mixtape, GStreamer, PyGObject and Python, and exit",
        ),
    ] = False,
) -> None:
    ctx.meta[_VERBOSE] = verbose


def _start(ctx: typer.Context, verbose: int, *, needs_gst: bool = True) -> int | None:
    """Set up logging for the `-v`s before and after the command. Returns None to go on, or the
    exit code when the command needs GStreamer and it is not there."""
    _setup_logging(verbose + ctx.meta.get(_VERBOSE, 0))
    if needs_gst and not _gst_available():
        print(NO_GST, file=sys.stderr)
        return EXIT_ERROR
    return None


@app.command(
    cls=_Command,
    short_help="run a pipeline until EOS or an error, like gst-launch-1.0 -e",
    help="Run a gst-launch style pipeline description until EOS or an error, or with --http "
    f"until interrupted.\n\n{TARGET_HELP}",
    epilog="Examples:\n\n\b\n"
    '  mixtape run "videotestsrc ! autovideosink"\n'
    "  mixtape run --http 8047 examples/demo.py:Demo\n"
    "\n"
    "Exit status: 0 at EOS or after a first Ctrl-C, 1 on a pipeline error, when --http "
    "cannot listen or without PyGObject, 2 on bad usage or a target or feature that cannot "
    "be loaded, 130 on a second Ctrl-C.",
)
def run(
    ctx: typer.Context,
    target: Target,
    assignments: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            parser=_assignment,
            metavar="PATH=VALUE",
            show_default=False,
            help="set a property before playing, e.g. enc.bitrate=2000; repeatable",
        ),
    ] = None,
    http: Annotated[
        HttpAddress | None,
        typer.Option(
            "--http",
            parser=_http_address,
            metavar="[HOST:]PORT",
            help="control the player from a browser (needs the http extra) and keep running "
            "after EOS or an error until interrupted; HOST defaults to 127.0.0.1",
        ),
    ] = None,
    expose_all: ExposeAll = False,
    feature: Features = None,
    verbose: Verbose = 0,
) -> int:
    if (code := _start(ctx, verbose)) is not None:  # before the extra: it imports gi too
        return code
    if http is not None:
        try:
            importlib.import_module("mixtape.interfaces.http")  # needs the extra
        except ImportError:
            _error("--http needs the http extra: pip install 'mixtape[http]'")
            return EXIT_USAGE
    player = _load_player(target, expose_all, feature or [])
    if player is None:
        return EXIT_USAGE
    return asyncio.run(_play(player, verbose + ctx.meta[_VERBOSE], _assignments(assignments), http))


def _assignments(texts: Sequence[str] | None) -> list[tuple[str, str]]:
    return [(path, value) for path, _, value in (text.partition("=") for text in texts or ())]


@app.command(
    cls=_Command,
    short_help="show the commands, properties and elements a player exposes",
    help="Show what a player exposes: its commands, properties and the properties of its "
    f"elements, with their types, ranges and current values.\n\n{TARGET_HELP}",
    epilog="Exit status: 0 on success, 1 without PyGObject, 2 on bad usage or a target that "
    "cannot be loaded, 130 on Ctrl-C.",
)
def inspect(
    ctx: typer.Context,
    target: Target,
    as_json: Annotated[bool, typer.Option("--json", help="print the model as JSON")] = False,
    expose_all: ExposeAll = False,
    feature: Features = None,
    verbose: Verbose = 0,
) -> int:
    if (code := _start(ctx, verbose)) is not None:
        return code
    return _inspect(target, as_json, expose_all, feature or [])


@perf_app.command(
    "compare",
    cls=_Command,
    short_help="find what got slower between two sessions or two runs",
    help="Compare two sessions of `pytest --mixtape-perf` (folders with an index.json) or two "
    "single runs (folders with a summary.json, as `mixtape run --feature perf,out=DIR` "
    "writes): the p95 and max of every metric where higher is worse. A value is a regression "
    "when it grew by more than the threshold and by more than a floor of its unit (100 µs, 5 "
    "points of CPU, 5% of a queue). Exits 1 on a regression.",
    epilog="Exit status: 0 when nothing got slower, 1 on a regression, 2 on a folder that "
    "cannot be compared or bad usage, 130 on Ctrl-C.",
)
def perf_compare(
    ctx: typer.Context,
    base: Annotated[Path, typer.Argument(metavar="BASE", help="the folder to compare with")],
    new: Annotated[Path, typer.Argument(metavar="NEW", help="the folder to check")],
    threshold: Annotated[
        float,
        typer.Option(
            "--threshold",
            parser=_percentage,
            metavar="PERCENT",
            show_default=False,
            help=f"how much a value may grow, in percent of the base; default {THRESHOLD_PERCENT}",
        ),
    ] = THRESHOLD_PERCENT,
    as_json: Annotated[bool, typer.Option("--json", help="print the comparison as JSON")] = False,
    verbose: Verbose = 0,
) -> int:
    _start(ctx, verbose, needs_gst=False)
    from .perf import compare

    try:
        result = compare.compare_folders(base, new, threshold / 100)
    except compare.CompareError as exc:
        _error(exc)
        return EXIT_USAGE
    print(json.dumps(result, indent=2) if as_json else compare.format_text(result))
    return EXIT_ERROR if result["regressions"] else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    if not sys.stderr.isatty() or os.environ.get("NO_COLOR"):
        os.environ.setdefault("GST_DEBUG_NO_COLOR", "1")  # GStreamer's logs, for a file or a pipe
    try:
        code = get_command(app).main(args=argv, prog_name="mixtape", standalone_mode=False)
    except UsageError as exc:
        _usage_error(exc, sys.argv[1:] if argv is None else argv)
        return exc.exit_code
    except ClickException as exc:
        _error(exc.format_message())
        return exc.exit_code
    except typer.Abort:
        return EXIT_INTERRUPTED
    return code if isinstance(code, int) else 0


def _usage_error(exc: Any, argv: Sequence[str]) -> None:
    """Print a usage error, and the usage line of the command that refused it."""
    ctx = exc.ctx
    if type(exc).__name__ == "NoArgsIsHelpError":  # a command with no words: its help
        print(ctx.get_help(), file=sys.stderr)
        return
    _error(exc.format_message().rstrip("."))  # a click message ends with a full stop
    if ctx is not None:
        print(ctx.get_usage(), file=sys.stderr)
    # an option that misses its value is refused before its command has a context: its words
    words = [arg for arg in argv if not arg.startswith("-")][:2]
    named = " ".join(words if words == ["perf", "compare"] else words[:1])
    fallback = f"mixtape {named}" if named in ("run", "inspect", "perf compare") else "mixtape"
    path = ctx.command_path if ctx is not None else fallback
    print(f"Try '{path} --help' for help.", file=sys.stderr)


def _gst_available() -> bool:
    try:
        importlib.import_module("mixtape.player")  # imports gi and GStreamer
    except (ImportError, ValueError):  # gi.require_version raises ValueError
        return False
    return True


_SENTENCE_STARTS = frozenset(
    "A Cannot Could Feature Got Interrupted Invalid Missing No Option The Unknown Use".split()
)
"""The words the texts of the library and of the command line parser start a sentence with:
their first letter is lower case after `error: `."""


def _error(message: object) -> None:
    """Print `error: message`, with the first word of a sentence in lower case. Only the words of
    `_SENTENCE_STARTS` are: a name such as `Player` stays."""
    first, space, rest = str(message).partition(" ")
    if first in _SENTENCE_STARTS:
        first = first.lower()
    print(f"error: {first}{space}{rest}", file=sys.stderr)


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


_PLAYER_CLASS = re.compile(r"(?P<module>.+\.py|[A-Za-z_][\w.]*):(?P<attribute>[A-Za-z_][\w.]*)")


def _load_player(words: Sequence[str], expose_all: bool, features: Sequence[str]) -> Player | None:
    """Build the player of the target: a description, or `module:Class` or `file.py:Class` and
    its own. Prints the error and returns None when that fails."""
    from .exceptions import ModelError, PlayerDescriptionError
    from .player import Player

    target = " ".join(words)
    first, *more = target.split(None, 1)
    try:
        if (named := _PLAYER_CLASS.fullmatch(first)) is None:
            return Player.from_description(target, expose_all=expose_all, features=features)
        if more:
            raise ValueError(
                f"{first} is a player class and takes no description words; "
                "quote a pipeline description instead"
            )
        cls = _import_player_class(named["module"], named["attribute"])
        if cls.description is None:
            raise ValueError(f"{first} has no description to build a player from")
        return cls.from_description(cls.description, expose_all=expose_all, features=features)
    except (ValueError, PlayerDescriptionError, ModelError) as exc:
        _error(exc)
        return None


def _import_player_class(module: str, attribute: str) -> type[Player]:
    """Import `module:Class` or `file.py:Class` (dotted attributes allowed); a ValueError says
    what is wrong. A file is imported as a module of its folder, which goes on `sys.path`."""
    from .player import Player

    path = Path(module) if module.endswith(".py") else None
    if path is None:
        name, folder = module, os.getcwd()  # like uvicorn, to import what is next to the user
    elif path.is_file():
        name, folder = path.stem, str(path.resolve().parent)
    else:
        raise ValueError(f"cannot import {module!r}: no such file")
    if folder not in sys.path:
        sys.path.insert(0, folder)
    try:
        value: Any = importlib.import_module(name)
    except Exception as exc:  # whatever the module does when it is imported
        raise ValueError(f"cannot import {module!r}: {exc}") from exc
    if path is not None and not path.resolve().samefile(getattr(value, "__file__", path)):
        raise ValueError(f"cannot import {module!r}: {name!r} is another module, {value.__file__}")
    for part in attribute.split("."):
        try:
            value = getattr(value, part)
        except AttributeError:
            raise ValueError(f"{module!r} has no attribute {attribute!r}") from None
    if not (isinstance(value, type) and issubclass(value, Player)):
        raise ValueError(f"{module}:{attribute} is not a Player subclass")
    return value


# -- inspect -- #


def _inspect(words: Sequence[str], as_json: bool, expose_all: bool, features: Sequence[str]) -> int:
    player = _load_player(words, expose_all, features)
    if player is None:
        return EXIT_USAGE
    spec = player.describe()
    if as_json:
        print(spec.model_dump_json(indent=2))
        return 0
    print(spec.name)
    if spec.features:
        print(f"Features: {', '.join(spec.features)}")
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
