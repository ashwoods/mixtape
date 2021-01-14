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
logger.setLevel(logging.INFO)


# flake8 plugin is way too verbose
def pytest_configure(config):
    logging.getLogger("flake8").setLevel(logging.ERROR)
    logging.getLogger("bandit").setLevel(logging.WARN)
    logging.getLogger("blib2to3").setLevel(logging.WARN)
    logging.getLogger("stevedore").setLevel(logging.WARN)
    logging.getLogger("filelock").setLevel(logging.WARN)


@pytest.fixture
def Gst():  # noqa
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst as GstCls

    GstCls.init(None)
    return GstCls


@pytest.fixture
def pipeline(Gst):
    """Make sure test pipeline is correct and test env setup"""
    SIMPLE_PIPELINE_DESCRIPTION = """videotestsrc ! queue ! fakesink"""
    pipeline = Gst.parse_launch(SIMPLE_PIPELINE_DESCRIPTION)
    assert isinstance(pipeline, Gst.Pipeline)
    return pipeline


@pytest.fixture
def player(Gst):
    from mixtape import Player

    return Player
