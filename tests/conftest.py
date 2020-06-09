# type: ignore
import logging
import colorlog
import pytest


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


# flake8 plugin is way too verbose
def pytest_configure(config):
    logging.getLogger("flake8").setLevel(logging.WARN)
    logging.getLogger("bandit").setLevel(logging.WARN)
    logging.getLogger("blib2to3").setLevel(logging.WARN)


@pytest.fixture
def Gst():  # noqa
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst as GstCls

    GstCls.init(None)
    return GstCls


@pytest.fixture
def player(Gst):
    from mixtape.players import AsyncPlayer

    return AsyncPlayer
