"""The samples of cli.md: see tests/doc_samples.py."""

import re

from tests.doc_samples import Sample, on, run

PAGE = "cli.md"


def url_line_then_124(out: str) -> None:
    """The system chooses the port of `--http 0`: any one does."""
    assert re.fullmatch(r'\{"url": "http://127\.0\.0\.1:\d+"\}\nexit status 124\n?', out), out


SAMPLES: list[Sample] = [
    run(PAGE, "help", "^mixtape --help", exact=True),
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
    run(PAGE, "ctrl-c", 'mixtape run "videotestsrc is-live=true ! filesink', exact=True),
    run(PAGE, "ctrl-c-hang", "done\nsleep 1\nkill -INT $!\nwait", exact=True),
    run(
        PAGE,
        "ctrl-c-twice",
        "done\nsleep 1\nkill -INT $!\nsleep 1",
        exact=True,
    ),
    run(PAGE, "timeout", '^mixtape run "videotestsrc is-live=true ! fakesink" --t', exact=True),
    # --- inspect
    run(PAGE, "inspect-demo", "^mixtape inspect examples/demo.py:Demo", exact=True),
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
        "^mixtape inspect --json examples/demo.py:Demo",
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
    run(PAGE, "set", "^mixtape run examples/demo.py:Demo --set", exact=True),
    run(PAGE, "set-bad-value", '^mixtape run "audiotestsrc num-buffers=10', exact=True),
    run(PAGE, "set-ambiguous", "^mixtape run --set q.max-size-buffers=5", exact=True),
    # --- http
    run(
        PAGE,
        "http",
        "^mixtape run --http 8047 examples/demo.py:Demo",
        hangs=True,
        timeout=6,
        exact=True,
    ),
    run(
        PAGE,
        "http-ready",
        "^mixtape run --http 0 --json --timeout 1",
        check=url_line_then_124,
        check_shown=True,
    ),
    # --- script it
    run(PAGE, "json-error", "^mixtape run --json --set src.patern=1", exact=True),
    run(PAGE, "events-jsonl", "^mixtape run --events jsonl --set src.pattern=ball", exact=True),
    # --- client
    run(PAGE, "client-get-set", "mixtape set 8047 vol.volume=0.5", exact=True),
    run(PAGE, "client-call", "mixtape call 8047 | grep shout", exact=True),
    run(PAGE, "client-watch", "mixtape watch 8047", exact=True),
    run(PAGE, "client-errors", "mixtape get 8047 vol.volum;", exact=True),
    run(
        PAGE,
        "client-unreachable",
        "^mixtape get 8047 vol.volume; echo",
        # the reason after the URL is the system's: [Errno 61] on macOS, 111 on Linux
        expect=(
            "error: cannot reach http://127.0.0.1:8047: [Errno ",
            "] Connection refused: is `mixtape run --http PORT` running there?\nexit status 1",
        ),
    ),
    # --- features
    run(PAGE, "unknown-feature", "^mixtape inspect --feature nope", exact=True),
    run(PAGE, "leaks", "^mixtape inspect --feature leaks,interval=0.5", exact=True),
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
        "^rm -f /tmp/ready\nmixtape run --http 8047 --json --feature gst:queue-levels",
        elements=("queue-levels",),
        exact=True,
    ),
    run(PAGE, "gst-leaks-refused", "^mixtape run --feature gst:leaks", exact=True),
    # --- compare
    run(PAGE, "perf-compare-help", "^mixtape perf compare --help", exact=True),
]
