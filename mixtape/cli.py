"""The `mixtape` command line."""

from __future__ import annotations

import asyncio
import contextlib
import enum
import errno
import importlib
import importlib.metadata
import json
import logging
import math
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

    from .client import ClientError
    from .events import EventStream
    from .exceptions import MixTapeError, PlayerPipelineError
    from .interfaces.messages import EventEncoder
    from .model import PropSpec
    from .player import Player

logger = logging.getLogger("mixtape")
_uvicorn_logger = logging.getLogger("uvicorn")

_log_handler: logging.Handler | None = None

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 124  # as timeout(1)
EXIT_INTERRUPTED = 130
HTTP_HOST = "127.0.0.1"
THRESHOLD_PERCENT = 50  # `perf.compare.THRESHOLD` as percent: a test keeps them in step
NO_GST = (
    "error: mixtape needs PyGObject and GStreamer 1.0 from the system Python: "
    "see https://shashi.to/mixtape/troubleshooting/"
)
INSPECT_HINT = "mixtape inspect TARGET lists them"
VERBOSE_HELP = "log more: -v info, -vv debug"
TARGET_HELP = (
    "TARGET is a pipeline description, as one quoted string or as its words, or a player "
    "class that has a description of its own: module:Class (a module in the current folder "
    "or on sys.path) or path/to/file.py:Class (a file: its folder is put on sys.path). A "
    "player class takes no further words. Its module is imported, which runs its code: "
    "keep what it does when run under `if __name__ == '__main__':`."
)
FEATURE_HELP = (
    "enable a feature with its options, e.g. leaks,interval=1; repeatable; a value has commas "
    "only inside parentheses"
)
_VERBOSE = "mixtape.verbose"  # the `-v`s before the command, in `Context.meta`

_as_json = False
"""Whether the command's `--json` asked for JSON: errors and warnings as JSON lines on stderr,
and no notices. `main` starts each run without."""


class EventFormat(enum.Enum):
    JSONL = "jsonl"


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


def _target(words: list[str]) -> list[str]:
    if not "".join(words).strip():  # `run ""`: words, but none to build from
        raise typer.BadParameter("it is empty")
    return words


Target = Annotated[list[str], typer.Argument(metavar="TARGET", hidden=True, callback=_target)]
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


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not (value > 0 and math.isfinite(value)):  # also nan
        raise typer.BadParameter(f"{text!r} is not a number of seconds, more than 0")
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
        _fail(NO_GST.removeprefix("error: "), "no_gstreamer")
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
    "be loaded, 124 when --timeout stopped it, 130 on a second Ctrl-C.",
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
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            parser=_seconds,
            metavar="SECONDS",
            show_default=False,
            help="stop with an EOS, as a first Ctrl-C does, once it played for SECONDS, and "
            "exit 124; with --http, stop serving too",
        ),
    ] = None,
    events: Annotated[
        EventFormat | None,
        typer.Option(
            "--events",
            show_default=False,
            help="print the model, the values and the graph, then every event, on stdout: "
            "jsonl, one JSON object per line, as the WebSocket of --http sends them",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="print errors as JSON on stderr and, with --http, the URL as JSON on stdout "
            "once listening",
        ),
    ] = False,
    expose_all: ExposeAll = False,
    feature: Features = None,
    verbose: Verbose = 0,
) -> int:
    global _as_json
    _as_json = as_json
    if (code := _start(ctx, verbose)) is not None:  # before the extra: it imports gi too
        return code
    if http is not None:
        try:
            importlib.import_module("mixtape.interfaces.http")  # needs the extra
        except ImportError:
            _fail("--http needs the http extra", "missing_extra", "pip install 'mixtape[http]'")
            return EXIT_USAGE
    player = _load_player(target, expose_all, feature or [])
    if player is None:
        return EXIT_USAGE
    play = _play(
        player,
        verbose + ctx.meta[_VERBOSE],
        _assignments(assignments),
        http,
        timeout=timeout,
        events=events is not None,
    )
    return asyncio.run(play)


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
    as_json: Annotated[
        bool, typer.Option("--json", help="print the model as JSON, and errors as JSON on stderr")
    ] = False,
    expose_all: ExposeAll = False,
    feature: Features = None,
    verbose: Verbose = 0,
) -> int:
    global _as_json
    _as_json = as_json
    if (code := _start(ctx, verbose)) is not None:
        return code
    return _inspect(target, as_json, expose_all, feature or [])


# -- the client of `run --http`: get, set, call, watch -- #


def _server_url(text: str) -> str:
    if text.startswith(("http://", "https://")):
        return text.rstrip("/")
    try:
        host, port = _http_address(text)
    except typer.BadParameter:
        raise typer.BadParameter(f"{text!r} is not a URL or [HOST:]PORT") from None
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def _pair(text: str) -> tuple[str, Any]:
    """The name and value of `NAME=VALUE`, whose value is a string, or of `NAME:=JSON`."""
    name, sep, value = text.partition("=")
    if not sep or name in ("", ":"):
        raise typer.BadParameter(f"{text!r} is not NAME=VALUE or NAME:=JSON")
    if not name.endswith(":"):
        return name, value
    try:
        return name[:-1], json.loads(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{text!r} has no JSON after ':=': {exc}") from None


def _pair_text(text: str) -> str:
    _pair(text)  # refused at once, as a usage error
    return text


ServerUrl = Annotated[
    str,
    typer.Argument(
        metavar="URL",
        parser=_server_url,
        show_default=False,
        help="the player, as `mixtape run --http` serves it: its URL, or [HOST:]PORT "
        "(HOST defaults to 127.0.0.1)",
    ),
]
VALUE_FORMS = (
    "VALUE is a string, which the server converts as it does those of `run --set`; "
    "{name}:=JSON gives a JSON value instead: {example}."
)
CLIENT_STATUS = (
    "1 when the server cannot be reached, does not answer as mixtape or cannot do it, "
    "130 on Ctrl-C."
)


def _client_failed(exc: ClientError) -> int:
    """Print what the server or the client refused, and return the exit code: 2 for a name or
    a value (404, 422), as `run` exits for a bad --set, else 1."""
    _fail(exc.message, exc.code, exc.hint)
    return EXIT_USAGE if exc.status in (404, 422) else EXIT_ERROR


def _shown(value: Any) -> str:
    """A value as text: a string as it is, for `$(mixtape get …)`, others as JSON."""
    return value if isinstance(value, str) else json.dumps(value)


@app.command(
    "get",
    cls=_Command,
    short_help="print property values of a player that `run --http` serves",
    help="Print the values of the properties at PATH, of a player that `mixtape run --http` "
    "serves: with one PATH its bare value, with several or none (every readable property) "
    "one `path = value` line each. A PATH is one that `mixtape inspect` lists: a property "
    "of the player (state) or of an element (vol.volume).",
    epilog="Examples:\n\n\b\n"
    "  mixtape get 8047\n"
    "  mixtape get 8047 vol.volume\n"
    "\n"
    f"Exit status: 0 on success, 2 on bad usage or a property it does not have, {CLIENT_STATUS}",
)
def client_get(
    ctx: typer.Context,
    url: ServerUrl,
    paths: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="PATH",
            show_default=False,
            help="a property, as `mixtape inspect` lists it; without any, every readable one",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="print one JSON object {path: value}, and errors as JSON"),
    ] = False,
    verbose: Verbose = 0,
) -> int:
    from .client import Client, ClientError

    global _as_json
    _as_json = as_json
    _start(ctx, verbose, needs_gst=False)
    client = Client(url)
    try:
        values = client.get(paths or client.paths())
    except ClientError as exc:
        return _client_failed(exc)
    if as_json:
        _print_json(values)
    elif paths and len(paths) == 1:
        print(_shown(values[paths[0]]))
    else:
        for path, value in values.items():
            print(f"{path} = {_shown(value)}")
    return 0


@app.command(
    "set",
    cls=_Command,
    short_help="set properties of a player that `run --http` serves",
    help="Set the properties of a player that `mixtape run --http` serves, in order, and stop "
    "at the first the server refuses: those before it stay set. A PATH is one that "
    f"`mixtape inspect` lists. {VALUE_FORMS.format(name='PATH', example='vol.mute:=true')}",
    epilog="Examples:\n\n\b\n"
    "  mixtape set 8047 vol.volume=0.5\n"
    "  mixtape set 8047 vol.mute:=true\n"
    "\n"
    "Exit status: 0 on success, 2 on bad usage or a property or value it refuses, "
    f"{CLIENT_STATUS}",
)
def client_set(
    ctx: typer.Context,
    url: ServerUrl,
    assignments: Annotated[
        list[str],
        typer.Argument(
            metavar="PATH=VALUE",
            parser=_pair_text,
            show_default=False,
            help="a property, as `mixtape inspect` lists it, and its value: PATH=VALUE or "
            "PATH:=JSON",
        ),
    ],
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="print the values as the server reads them back, as one JSON object "
            "{path: value}, and errors as JSON",
        ),
    ] = False,
    verbose: Verbose = 0,
) -> int:
    from .client import Client, ClientError

    global _as_json
    _as_json = as_json
    _start(ctx, verbose, needs_gst=False)
    try:
        values = Client(url).set([_pair(text) for text in assignments])
    except ClientError as exc:
        return _client_failed(exc)
    if as_json:
        _print_json(values)
    return 0


@app.command(
    "call",
    cls=_Command,
    short_help="run a command of a player that `run --http` serves",
    help="Run the command NAME of a player that `mixtape run --http` serves, with its "
    "arguments as NAME=VALUE, and print its result as JSON (nothing for null). Without NAME, "
    "list the commands, and those that cannot run now with why. "
    + VALUE_FORMS.format(name="NAME", example="eos:=true, times:=3, timeout:=null"),
    epilog="Examples:\n\n\b\n"
    "  mixtape call 8047\n"
    "  mixtape call 8047 pause\n"
    "  mixtape call 8047 stop eos:=true timeout:=null\n"
    "\n"
    "Exit status: 0 on success, 2 on bad usage or a command or argument it refuses, "
    f"{CLIENT_STATUS}",
)
def client_call(
    ctx: typer.Context,
    url: ServerUrl,
    name: Annotated[
        str | None,
        typer.Argument(
            metavar="NAME", show_default=False, help="the command; without it, list them"
        ),
    ] = None,
    args: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="ARG",
            parser=_pair_text,
            show_default=False,
            help="an argument of the command: NAME=VALUE or NAME:=JSON",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help='print {"result": …}, or the commands and the reasons of those that cannot '
            "run as the model has them, and errors as JSON",
        ),
    ] = False,
    verbose: Verbose = 0,
) -> int:
    from .client import Client, ClientError

    global _as_json
    _as_json = as_json
    _start(ctx, verbose, needs_gst=False)
    client = Client(url)
    try:
        if name is None:
            _list_commands(client.model(), as_json)
            return 0
        result = client.call(name, dict(_pair(text) for text in args or ()))
    except ClientError as exc:
        return _client_failed(exc)
    if as_json:
        _print_json({"result": result})
    elif result is not None:
        print(json.dumps(result))
    return 0


def _list_commands(model: dict[str, Any], as_json: bool) -> None:
    """Print the commands of a model as `inspect` does, then those that cannot run now."""
    unavailable = model.get("unavailable", {})
    if as_json:
        _print_json({"commands": model["commands"], "unavailable": unavailable})
        return
    commands = model["commands"].values()
    rows = [_command_row(c["name"], c["parameters"], c["description"]) for c in commands]
    print(_table(rows, cap=64))
    if unavailable:
        print("\nUnavailable now:")
        print(_table([[name, reason] for name, reason in unavailable.items()]))


@app.command(
    "watch",
    cls=_Command,
    short_help="print the events of a player that `run --http` serves",
    help="Print what the WebSocket /api/events of a player that `mixtape run --http` serves "
    "sends, one JSON object per line as `run --events jsonl` prints them: the model, the "
    "values and the graph, then every event, until the server closes it.",
    epilog="Examples:\n\n\b\n"
    "  mixtape watch 8047\n"
    "\n"
    "Exit status: 0 once the server closes the connection, 2 on bad usage or without the "
    f"http extra, {CLIENT_STATUS}",
)
def client_watch(
    ctx: typer.Context,
    url: ServerUrl,
    as_json: Annotated[bool, typer.Option("--json", help="print errors as JSON on stderr")] = False,
    verbose: Verbose = 0,
) -> int:
    from .client import ClientError, watch

    global _as_json
    _as_json = as_json
    _start(ctx, verbose, needs_gst=False)
    try:
        importlib.import_module("websockets")
    except ImportError:
        _fail("watch needs the http extra", "missing_extra", "pip install 'mixtape[http]'")
        return EXIT_USAGE
    try:
        asyncio.run(watch(url))
    except ClientError as exc:
        return _client_failed(exc)
    return 0


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
    as_json: Annotated[
        bool, typer.Option("--json", help="print the comparison and errors as JSON")
    ] = False,
    verbose: Verbose = 0,
) -> int:
    global _as_json
    _as_json = as_json
    _start(ctx, verbose, needs_gst=False)
    from .perf import compare

    try:
        result = compare.compare_folders(base, new, threshold / 100)
    except compare.CompareError as exc:  # a folder that is not a session or a run
        _fail(exc, "bad_target")
        return EXIT_USAGE
    print(json.dumps(result, indent=2) if as_json else compare.format_text(result))
    return EXIT_ERROR if result["regressions"] else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    global _as_json
    _as_json = False  # a command's --json sets it
    if not sys.stderr.isatty() or os.environ.get("NO_COLOR"):
        os.environ.setdefault("GST_DEBUG_NO_COLOR", "1")  # GStreamer's logs, for a file or a pipe
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        code = get_command(app).main(args=args, prog_name="mixtape", standalone_mode=False)
    except UsageError as exc:
        _as_json = _as_json or "--json" in args  # refused before its command ran
        _usage_error(exc, args)
        return exc.exit_code
    except ClickException as exc:
        _as_json = _as_json or "--json" in args
        _fail(exc.format_message(), "usage")
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
    message = exc.format_message().rstrip(".")  # a click message ends with a full stop
    # an option that misses its value is refused before its command has a context: its words
    words = [arg for arg in argv if not arg.startswith("-")][:2]
    named = " ".join(words if words == ["perf", "compare"] else words[:1])
    commands = ("run", "inspect", "get", "set", "call", "watch", "perf compare")
    fallback = f"mixtape {named}" if named in commands else "mixtape"
    path = ctx.command_path if ctx is not None else fallback
    if _as_json:
        _fail(message, "usage", f"'{path} --help' shows the usage")
        return
    _error(message)
    if ctx is not None:
        print(ctx.get_usage(), file=sys.stderr)
    print(f"Try '{path} --help' for help.", file=sys.stderr)


def _gst_available() -> bool:
    """Whether PyGObject and GStreamer 1.0 are there. Any other ImportError, of mixtape's own
    modules or what they import, is not theirs to explain: it is raised where it happens."""
    try:
        import gi

        gi.require_version("Gst", "1.0")  # raises ValueError without GStreamer
        from gi.repository import Gst

        Gst.IteratorError  # noqa: B018  # from gst-python's overrides, which mixtape needs too
    except (ImportError, ValueError, AttributeError):
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


def _fail(message: object, code: str, hint: str | None = None) -> None:
    """Print an error: `error: message: hint`, or with `--json` one line
    `{"error": {"code", "message", "hint"}}`, whose code is one of the library's (`exc.code`)
    or of the command line's own."""
    if _as_json:
        _print_json({"error": {"code": code, "message": str(message), "hint": hint}}, err=True)
    else:
        _error(f"{message}: {hint}" if hint else message)


def _fail_on(exc: MixTapeError) -> None:
    """Print a library error, with its code and hint."""
    from .exceptions import UnknownNameError

    _fail(exc, exc.code, exc.hint if isinstance(exc, UnknownNameError) else None)


def _pipeline_error(error: PlayerPipelineError, verbose: int) -> None:
    """Print a pipeline error, and its debug string with `-v`."""
    message = f"{error.source}: {error.message}"
    if _as_json:
        _fail(message, error.code)
    else:  # not as `_error`: an element's name stays as it is
        print(f"error: {message}", file=sys.stderr)
    if verbose and error.debug:
        print(error.debug, file=sys.stderr)


def _notice(text: str) -> None:
    """Print what a person at the terminal wants to know on stderr; not with `--json`."""
    if not _as_json:
        print(text, file=sys.stderr)


def _print_json(message: dict[str, Any], *, err: bool = False) -> None:
    """Print `message` as one line of JSON, on stdout or with `err` on stderr, at once: a
    program reads it as it comes."""
    from .interfaces.encoding import encode_json  # without PyGObject too

    print(encode_json(message), file=sys.stderr if err else sys.stdout, flush=True)


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
        return cls.from_description(expose_all=expose_all, features=features)
    except (PlayerDescriptionError, ModelError) as exc:
        _fail_on(exc)
        return None
    except ValueError as exc:
        _fail(exc, "bad_target")
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
        rows = [_command_row(c.name, c.parameters, c.description) for c in spec.commands.values()]
        print(_table(rows, cap=64))
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


def _command_row(name: str, parameters: dict[str, Any], description: str | None) -> list[str]:
    """A command's row: its signature, from the JSON Schema of its `parameters`, and what it
    does. Of a `CommandSpec`, or of its JSON as a client reads it from the model."""
    required = set(parameters.get("required", ()))
    params = []
    for param, schema in parameters.get("properties", {}).items():
        text = f"{param}: {_type(schema, keep_null=True)}"
        if param not in required:
            text += f" = {_value(schema.get('default'))}"
        params.append(text)
    return [f"{name}({', '.join(params)})", description or ""]


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
    *,
    timeout: float | None = None,
    events: bool = False,
) -> int:
    """Play until EOS, stopping with an EOS on the first SIGINT or SIGTERM, or once `timeout`
    seconds have passed, and at once on the second. Returns the exit code.

    With `http` (host, port) it serves the player instead, and neither an EOS nor an
    error ends it: only the first interrupt or the timeout does, once the player has stopped.
    With `events`, it prints the snapshot and then every event as JSON lines on stdout."""
    from gi.repository import Gst

    from .exceptions import (
        ModelError,
        PlayerPipelineError,
        PlayerSetStateError,
        UnknownNameError,
    )
    from .interfaces.messages import EventEncoder

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    interrupts = 0
    timed_out = False
    stopping: asyncio.Task[None] | None = None
    interrupted = asyncio.Event()

    def interrupt() -> None:
        nonlocal interrupts, stopping
        interrupts += 1
        if interrupts == 1:
            _notice(f"{'Timeout' if timed_out else 'Interrupt'}: stopping (EOS)…")
            stopping = loop.create_task(player.stop(eos=True))
            interrupted.set()
        elif interrupts == 2:
            # The teardown that follows waits for a hung pipeline for up to its
            # `state_timeout`: a third Ctrl-C then kills the process, as the default does.
            _notice("Interrupt: stopping now (Ctrl-C again to kill)…")
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            if main_task is not None:
                main_task.cancel()

    def expire() -> None:
        nonlocal timed_out
        if not interrupts:  # after a Ctrl-C, it already stops
            timed_out = True
            interrupt()

    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, interrupt)
    pump: asyncio.Task[None] | None = None
    stream: EventStream | None = None
    printer: asyncio.Task[None] | None = None
    timer: asyncio.TimerHandle | None = None
    try:
        if sys.platform == "darwin":
            from . import macos

            pump = macos.start_pump()  # before the pipeline leaves NULL
        async with player:
            for path, value in assignments:
                try:
                    await player.set(path, value)
                except UnknownNameError as exc:
                    if str(exc) == f"Unknown property {path!r}":  # not ambiguous or hidden
                        exc.hint = exc.hint or INSPECT_HINT
                    raise
            if events:
                stream = player.events(position=True)  # before the snapshot, as the socket's
                encoder = EventEncoder(player)
                for message in encoder.snapshot():  # now: the values from before it plays
                    _print_json(message)
                printer = loop.create_task(_print_events(encoder, stream))
            if timeout is not None:
                timer = loop.call_later(timeout, expire)
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
        _fail_on(exc)
        return EXIT_USAGE
    except PlayerPipelineError as exc:
        _pipeline_error(exc, verbose)
        return EXIT_ERROR
    except OSError as exc:  # --http cannot listen
        if http is None:
            raise
        if exc.errno == errno.EADDRINUSE:
            _fail(
                f"{http[0]}:{http[1]} is in use (is another mixtape run --http running?)",
                "address_in_use",
                "choose another port with --http PORT",
            )
        else:
            _fail(exc, "listen_failed")
        return EXIT_ERROR
    except PlayerSetStateError as exc:
        if interrupts and player.state == Gst.State.NULL:  # the stop ended a wait, as asked
            return EXIT_TIMEOUT if timed_out else 0
        _fail_on(exc)
        return EXIT_ERROR
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        if timer is not None:
            timer.cancel()
        if stream is not None and printer is not None and interrupts < 2:
            stream.close()  # the teardown has closed it: the events it queued still print
            await printer
        for task in (pump, stopping, printer):
            if task is not None:
                task.cancel()
        if stopping is not None and stopping.done() and not stopping.cancelled():
            stopping.exception()  # the teardown raised it again, or it did not matter
    return EXIT_TIMEOUT if timed_out else 0


async def _print_events(encoder: EventEncoder, stream: EventStream) -> None:
    """Print the events of `stream` until it ends, as JSON lines in the shapes of the
    WebSocket's messages."""
    from .interfaces.encoding import encode_json

    async for event in stream:
        if (message := encoder.encode(event)) is None:
            continue
        try:
            text = encode_json(message)
        except ValueError:  # a value JSON cannot carry: as the socket, the others still print
            logger.exception("Could not print %r", event)
            continue
        print(text, flush=True)


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
        warning = (
            f"serving on {host} without authentication: anyone who can reach it "
            "can control the player"
        )
        if _as_json:
            _print_json({"warning": {"code": "unauthenticated", "message": warning}}, err=True)
        else:
            print(f"warning: {warning}", file=sys.stderr)
    log_level = ("warning", "info", "debug")[min(verbose, 2)]
    async with serve(player, host, port, log_level=log_level) as listening:
        if _as_json:
            _print_json({"url": listening.url})  # it is ready: a program can connect
        else:
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
                    _fail_on(exc)
            yield
        finally:
            stream.close()
            reporter.cancel()


async def _report_errors(stream: EventStream, verbose: int) -> None:
    """Print the pipeline errors."""
    from .events import Error

    async for event in stream:
        if isinstance(event, Error):
            _pipeline_error(event.error, verbose)
