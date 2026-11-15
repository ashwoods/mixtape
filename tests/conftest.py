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
    description = "videotestsrc ! queue ! fakesink"
    pipeline = Gst.parse_launch(description)
    assert isinstance(pipeline, Gst.Pipeline)
    return pipeline


@pytest.fixture
def error_pipeline(Gst):
    """Error pipeline"""
    description = "filesrc ! queue ! fakesink"
    pipeline = Gst.parse_launch(description)
    assert isinstance(pipeline, Gst.Pipeline)
    return pipeline
