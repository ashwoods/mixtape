"""Make video windows appear on macOS under plain asyncio.

macOS only shows a window when the main thread's CoreFoundation run loop is
serviced, and neither asyncio, `GLib.MainLoop` nor `gi.events` ever does that.
Without it `autovideosink` (glimagesink) renders frames but no window appears,
and `osxvideosink` blocks in NULL to READY.

`start_pump` creates the shared `NSApplication` through ctypes and starts an
asyncio task that runs one non-blocking turn of the main run loop every 10 ms.
It must run before the pipeline leaves NULL. Import this module on macOS only.
"""

import asyncio
import ctypes
import ctypes.util

INTERVAL = 0.01
"""Seconds between two turns of the run loop"""


def _load(name: str) -> ctypes.CDLL:
    path = ctypes.util.find_library(name)
    if path is None:
        raise RuntimeError(f"Cannot find the {name} library")
    return ctypes.CDLL(path)


def start_pump() -> "asyncio.Task[None]":
    """Create the shared `NSApplication` and pump the main run loop on the running loop.

    Cancel the returned task to stop pumping.
    """
    core_foundation = _load("CoreFoundation")
    core_foundation.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
    default_mode = ctypes.c_void_p.in_dll(core_foundation, "kCFRunLoopDefaultMode")
    objc = _load("objc")
    _load("AppKit")  # registers the NSApplication class
    objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
    send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
        ("objc_msgSend", objc)
    )
    send(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))

    async def pump() -> None:
        while True:
            core_foundation.CFRunLoopRunInMode(default_mode, 0.0, False)
            await asyncio.sleep(INTERVAL)

    return asyncio.get_running_loop().create_task(pump())
