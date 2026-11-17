"""The `perf` feature needs GStreamer's tracers, which only exist when GStreamer is first
initialised by mixtape: something that initialised it earlier is refused.

python3 examples/perf_late.py
"""

# --8<-- [start:late]
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)  # something initialises GStreamer before mixtape does

from mixtape import Player
from mixtape.exceptions import FeatureError

try:
    Player.from_description("videotestsrc ! fakesink", features=["perf"])
except FeatureError as error:
    print(error)
# --8<-- [end:late]
