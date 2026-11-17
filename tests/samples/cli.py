"""The samples of cli.md: see tests/doc_samples.py."""

from tests.doc_samples import Sample, on, run

PAGE = "cli.md"

SAMPLES: list[Sample] = [
    # --- run
    run(PAGE, "words", "^mixtape run videotestsrc num-buffers=60", exact=True),
    run(PAGE, "bad-description", '^mixtape run "videotestsrc ! nosuchelement', exact=True),
    run(PAGE, "pipeline-error", '^mixtape run "filesrc location=/tmp/missing.mp4', exact=True),
    run(
        PAGE,
        "verbose-debug",
        "^mixtape run -v ",
        expect=("No such file",),
    ),
    run(PAGE, "ctrl-c", '^mixtape run "videotestsrc is-live=true', exact=True),
    run(
        PAGE,
        "ctrl-c-hang",
        '^mixtape run "videotestsrc ! identity sleep-time=30000000 ! fakesink" &\n'
        "sleep 2\nkill -INT $!\nwait",
        exact=True,
    ),
    run(
        PAGE,
        "ctrl-c-twice",
        '^mixtape run "videotestsrc ! identity sleep-time=30000000 ! fakesink" &\n'
        "sleep 2\nkill -INT $!\nsleep 1",
        exact=True,
    ),
    # --- inspect
    run(
        PAGE,
        "inspect-demo",
        "^cd examples && mixtape inspect demo:Demo",
        expect=(
            "Demo\n",
            "shout(word: string, times: integer = 2)",
            "Repeat a word.",
            "→ vol.volume",
            "vol (volume) — Volume",
            "mute",
        ),
    ),
    run(
        PAGE,
        "mutable",
        '^mixtape inspect "videotestsrc ! x264enc',
        elements=("x264enc",),
        expect=("mutable: playing", "bitrate"),
    ),
    run(
        PAGE,
        "inspect-json",
        "^cd examples && mixtape inspect --json",
        expect=('"target": "vol.volume"', '"mutable": null', '"minimum": 0.0'),
    ),
    run(PAGE, "named", '^mixtape inspect "videotestsrc name=src', exact=True),
    run(PAGE, "expose-all", "^mixtape inspect --expose-all", exact=True),
    run(PAGE, "namesakes", '^mixtape inspect "bin.(', exact=True),
    run(
        PAGE,
        "action-signals",
        '^mixtape inspect "appsrc name=src',
        expect=(
            "src.end-of-stream()",
            "Emit the action signal 'end-of-stream' of GstAppSrc",
        ),
    ),
    # --- set
    run(PAGE, "set", "^cd examples && mixtape run demo:Demo --set", exact=True),
    run(PAGE, "set-bad-value", '^mixtape run "audiotestsrc num-buffers=10', exact=True),
    run(PAGE, "set-ambiguous", "^mixtape run --set q.max-size-buffers=5", exact=True),
    # --- http
    run(
        PAGE,
        "http",
        "^cd examples && mixtape run --http 8047 demo:Demo",
        hangs=True,
        timeout=6,
        exact=True,
    ),
    # --- features
    run(PAGE, "unknown-feature", "^mixtape inspect --feature nope", exact=True),
    run(
        PAGE,
        "leaks",
        "^mixtape inspect --feature leaks,interval=0.5",
        expect=(
            "Features: leaks\n",
            "leaks.checkpoint()",
            "The change in live objects by type since the last checkpoint",
            "leaks.reset()",
            "leaks.alive  r-  object",
        ),
    ),
    run(
        PAGE,
        "tracers-active",
        "^mixtape inspect --feature 'tracers,names=latency",
        expect=("tracers.active", "= latency+rusage"),
    ),
    run(PAGE, "tracers-run", '^mixtape run "videotestsrc num-buffers=30', exact=True),
    run(
        PAGE,
        "gst-latency",
        "^mixtape inspect --feature gst:latency,flags=element",
        # the latency tracer's flags property is new in GStreamer 1.26 (Ubuntu 24.04 has 1.24)
        expect=(
            "Features: gst:latency\n",
            "gst:latency.params",
            *on("darwin", "gst:latency.flags", "= element"),
        ),
    ),
    run(
        PAGE,
        "gst-queue-levels",
        "^mixtape inspect --feature gst:queue-levels",
        elements=("queue-levels",),
        expect=(
            "gst:queue-levels.write-log(arg1: string | null)",
            "Emit the action signal 'write-log' of GstQueueLevels",
        ),
    ),
    run(
        PAGE,
        "gst-write-log",
        "^mixtape run --http 8047 --feature gst:queue-levels",
        elements=("queue-levels",),
        exact=True,
    ),
    run(PAGE, "gst-leaks-refused", "^mixtape run --feature gst:leaks", exact=True),
]
