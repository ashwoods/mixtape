"""Helpers of the player and its state machine: GStreamer iterators and calls on a thread."""

import concurrent.futures
import threading
from collections.abc import Callable, Iterable
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


def _retrying[T](iterate: Callable[[], Iterable[T]]) -> list[T]:
    """The items of a GStreamer iterator, walked again when it RESYNCs: the bin or element
    changed under it, from a streaming thread."""
    while True:
        try:
            return list(iterate())
        except Gst.IteratorError:
            continue


def _in_thread[T](
    fn: Callable[..., T], *args: object, after: concurrent.futures.Future[Any] | None = None
) -> concurrent.futures.Future[T]:
    """Call `fn(*args)` on a daemon thread, once `after` is done: a call blocked in
    GStreamer (a hung streaming thread holds its locks) blocks neither the loop nor the exit."""
    future: concurrent.futures.Future[T] = concurrent.futures.Future()

    def run() -> None:
        if after is not None:
            concurrent.futures.wait([after])
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(fn(*args))
            except BaseException as exc:
                future.set_exception(exc)

    threading.Thread(target=run, name="mixtape", daemon=True).start()
    return future


def _names(elements: Iterable[Gst.Element]) -> str:
    return ", ".join(element.get_name() for element in elements)
