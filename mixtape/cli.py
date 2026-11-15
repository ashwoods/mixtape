"""The `mixtape` command line."""

import argparse
import asyncio
import importlib.metadata
import logging
import signal
import sys
from collections.abc import Callable, Sequence

from .exceptions import PlayerDescriptionError, PlayerPipelineError, PlayerSetStateError
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
    run.add_argument("description", nargs="+", metavar="DESCRIPTION", help="the pipeline")
    run.set_defaults(handler=_run)
    return parser


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
    logger.addHandler(_log_handler)
    logger.setLevel(level)


def _run(args: argparse.Namespace) -> int:
    try:
        player = Player.from_description(" ".join(args.description))
    except PlayerDescriptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return asyncio.run(_play(player, args.verbose))


async def _play(player: Player, verbose: int) -> int:
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
        elif main_task is not None:
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
            await player.play_until_eos()
    except asyncio.CancelledError:
        if interrupts < 2:
            raise
        return EXIT_INTERRUPTED
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
