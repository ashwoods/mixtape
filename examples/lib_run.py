"""`Player.run`: play a description until it ends, for scripts.

python3 examples/lib_run.py
"""

# --8<-- [start:run]
from mixtape import Player

Player.run("videotestsrc num-buffers=100 ! fakesink", timeout=30)
print("done")
# --8<-- [end:run]
