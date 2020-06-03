import asyncio
import logging
import enum
import colorlog  # type: ignore
import pytest  # type: ignore

from pathlib import Path

logger = logging.getLogger(__name__)
handler = colorlog.StreamHandler()
handler.setFormatter(
    colorlog.ColoredFormatter(
        "(%(asctime)s) [%(log_color)s%(levelname)s] | %(name)s | %(message)s [%(threadName)-10s]"
    )
)

# get root logger
logger = logging.getLogger()
logger.handlers = []
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)


@pytest.fixture
def Gst():
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst as GstCls

    GstCls.init(None)
    return GstCls


@pytest.fixture
def player(Gst):
    from mixtape.players import AsyncPlayer

    return AsyncPlayer
