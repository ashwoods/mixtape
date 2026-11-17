"""The pipeline graphs of the pages: see tests/graphs.py. Where a program of examples/ assigns
its description, the graph reads it from there."""

from tests.doc_samples import Sample, graph
from tests.graphs import literal, module

publisher = module("webrtc_client.py").publisher

# the bin a client adds for a call (examples/webrtc_client.py `call`), with its default sink
CALL = " ( webrtcsrc name=in stun-server=NULL ! videoconvert name=convert ! fakesink name=display )"
WEBRTC = ("webrtcsink", "webrtcsrc")

SAMPLES: list[Sample] = [
    graph("index.md", "hello", literal("hello.py", "description")),
    graph("cli.md", "demo", literal("demo.py", "description")),
    graph("http.md", "demo", literal("demo.py", "description")),
    graph(
        "http.md",
        "shape",
        ("the recorder", "audiotestsrc num-buffers=5 ! wavenc ! filesink location=/tmp/tone.wav"),
        ("the player", "filesrc location=/tmp/tone.wav ! decodebin name=dec ! fakesink"),
        links=(("0.filesink", "1.filesrc", "tone.wav"), ("1.dec", "1.fakesink", "once decoded")),
    ),
    graph("library.md", "lifecycle", "videotestsrc is-live=true ! fakesink"),
    graph("library.md", "dynamic", "videotestsrc name=src num-buffers=30 ! fakesink name=sink"),
    graph("library.md", "errors", literal("lib_errors.py", "description")),
    graph("library.md", "events", literal("lib_events.py", "description")),
    # with the bin that lib_shape adds while it plays
    graph(
        "library.md",
        "shape",
        literal("lib_shape.py", "DESCRIPTION") + " t. ! ( queue ! fakesink name=extra )",
    ),
    graph("library.md", "testcard", literal("testcard.py", "description")),
    graph("library.md", "heartbeat", literal("heartbeat_plugin/heartbeat_demo.py", "description")),
    graph(
        "perf.md",
        "recorder",
        "videotestsrc name=src is-live=true num-buffers=90"
        " ! video/x-raw,width=1280,height=720,framerate=30/1 ! queue name=q"
        " ! x264enc name=enc speed-preset=slow threads=1 bitrate=4000"
        " ! mp4mux name=mux ! filesink name=sink location=/tmp/rec.mp4",
        elements=("x264enc",),
    ),
    graph("webrtc.md", "publish", publisher("alice"), elements=WEBRTC),
    graph(
        "webrtc.md",
        "call",
        ("alice", publisher("alice", serve=True) + CALL),
        ("bob", publisher("bob") + CALL),
        links=(
            ("0.out", "1.in", "WebRTC"),
            ("1.out", "0.in", "WebRTC"),
            ("0.in", "0.convert", "on a call"),
            ("1.in", "1.convert", "on a call"),
        ),
        direction="TB",
        elements=WEBRTC,
    ),
    graph(
        "two-pipelines.md",
        "inter",
        ("producer", literal("inter_restart.py", "PRODUCER")),
        ("consumer", literal("inter_restart.py", "CONSUMER")),
        links=(("0.intervideosink", "1.intervideosrc", "channel cam"),),
    ),
    graph(
        "two-pipelines.md",
        "proxy",
        ("producer", literal("proxy_link.py", "PRODUCER")),
        ("consumer", literal("proxy_link.py", "CONSUMER")),
        links=(("0.out", "1.in", "proxysink property"),),
    ),
    graph(
        "two-pipelines.md",
        "unixfd",
        (
            "producer, :8101",
            "videotestsrc is-live=true name=cam ! unixfdsink socket-path=/tmp/cam.sock",
        ),
        (
            "consumer, :8102",
            "unixfdsrc name=feed socket-path=/tmp/cam.sock ! videoconvert name=conv ! fakesink",
        ),
        links=(("0.unixfdsink", "1.feed", "cam.sock"),),
        elements=("unixfdsink",),
    ),
    graph(
        "two-pipelines.md",
        "two-players",
        ("cam", literal("two_players.py", "CAM")),
        ("mic", literal("two_players.py", "MIC")),
    ),
]
