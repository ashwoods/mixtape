# Troubleshooting

Each heading is the text of an error. Find yours, and the fix is under it.

## ModuleNotFoundError: No module named 'gi' { #no-module-named-gi }

The Python that runs your program has no PyGObject: it comes from your system, not from PyPI, and
a venv does not see it unless you ask.

Make the venv from a Python that has it, with `--system-site-packages`. This command must
succeed with the Python you start from, and print nothing:

```console
$ python3 -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.IteratorError'
```

Homebrew's Python is `$(brew --prefix)/bin/python3` and Ubuntu's is `/usr/bin/python3`. Then
`python3 -m venv --system-site-packages .venv`. [Install](index.md#install) has the steps.

## error: mixtape needs PyGObject and GStreamer 1.0 from the system Python: see https://shashi.to/mixtape/troubleshooting/ { #mixtape-needs-pygobject }

`mixtape run` and `mixtape inspect` could not import PyGObject, GStreamer or gst-python's
overrides, so they stopped with exit status 1. gst-python is the part that makes GStreamer's
classes usable from Python: Homebrew's `gstreamer` includes it, and on Ubuntu it is
`python3-gst-1.0`. The cause and the fix are those of
[the `gi` error above](#no-module-named-gi): install GStreamer, PyGObject and gst-python on the
system, and make the venv from a Python that has them.

## no element "x264enc" { #no-element-x264enc }

The pipeline needs `x264enc`, which is in gst-plugins-ugly, and this GStreamer does not have that
plugin. `mixtape run` prints it as the last words of an invalid pipeline, after the description, and
`Player.from_description` raises `PlayerDescriptionError` with the same words:
`error: invalid pipeline 'videotestsrc ! x264enc ! fakesink': no element "x264enc"`.

Homebrew's `gstreamer` has the plugin. On Ubuntu, `sudo apt install gstreamer1.0-plugins-ugly`.
Check that the element is there:

```console
$ gst-inspect-1.0 --exists x264enc && echo found
found
```

[Measuring performance](perf.md) records with `x264enc`. Any other element named in this message
is in another plugin: `gst-inspect-1.0 NAME`, run on a machine that has it, says which.

## no element "webrtcsink" { #no-element-webrtcsink }

`webrtcsink` and `webrtcsrc` are in gst-plugins-rs, which Ubuntu does not package. Homebrew's
`gstreamer` includes it. On Ubuntu, build it with `scripts/build-rs-plugins` and set
`GST_PLUGIN_PATH`: [A WebRTC client](webrtc.md#install) has the commands, and the check that both
elements are there.

## initialised before it could set GST_TRACERS { #gst-tracers }

The message starts `The feature 'perf' needs the tracers …` (or `The feature 'leaks' needs the
leaks tracer …`) and ends `…before anything else initialises GStreamer, or set GST_TRACERS
yourself`. GStreamer [creates its tracers once per process](concepts.md#tracers-once-per-process),
when it initialises, and something in your program initialised it before the feature could ask
for them.

Build the player with `Player.from_description(..., features=['perf'])` before anything else
touches GStreamer, or run `mixtape run --feature perf`, which does. If another library initialises
GStreamer first, set `GST_TRACERS` yourself in the environment.
[First in the process](perf.md#first-in-the-process) shows the program and the full message.

## error: 127.0.0.1:8047 is in use (is another mixtape run --http running?): choose another port with --http PORT { #port-in-use }

Something already listens on that port, often a `mixtape run --http` that is still running. The
command exits 1. Stop the other one, or choose another port. This one stands in for it with a
server on the port; it stops the server on its last line:

```console
$ python3 -m http.server 8047 --bind 127.0.0.1 > /dev/null 2>&1 &
$ until curl -s 127.0.0.1:8047 > /dev/null; do sleep 0.1; done
$ mixtape run --http 8047 "videotestsrc ! fakesink"; echo "exit status $?"; kill $!
error: 127.0.0.1:8047 is in use (is another mixtape run --http running?): choose another port with --http PORT
exit status 1
```

`lsof -i :8047` names the process that holds the port. The [command line](cli.md#the-http-option)
page has the options of `--http`, and the port `0` of [`serve()`](http.md#in-code) asks for a
free one.

## error: cannot reach http://127.0.0.1:8047: [Errno 61] Connection refused: is `mixtape run --http PORT` running there? { #cannot-reach }

`mixtape get`, `set` or `call` found nothing listening at that address, and exited 1, with the
code `connection_failed`. The reason after the URL is the system's: `[Errno 111]` on Linux.
`mixtape watch` words it as the system does: `Connect call failed`.
The player is not running, or it listens on another port or host. Start it with
`mixtape run --http PORT`, and give the client the address it printed (`Serving on …`, or
`{"url": …}` with `--json`). Here the first command finds no server, and the last, once one runs,
works:

```console
$ mixtape get 8047 vol.volume; echo "exit status $?"
$ rm -f /tmp/ready
$ mixtape run --http 8047 --json "audiotestsrc is-live=true ! volume name=vol ! fakesink" > /tmp/ready 2>/dev/null &
$ until [ -s /tmp/ready ] || ! kill -0 $!; do sleep 0.2; done
$ mixtape get 8047 vol.volume; kill -INT $!; wait
error: cannot reach http://127.0.0.1:8047: [Errno 61] Connection refused: is `mixtape run --http PORT` running there?
exit status 1
1.0
```

If the address answers but is not mixtape's, the error is `did not answer as mixtape does`
(`bad_response`): the port belongs to another server. The [client
commands](cli.md#client) have the rest.

## mixtape run exits with status 124 { #timeout-124 }

`mixtape run --timeout SECONDS` stopped the pipeline when the time was up, with an EOS as a first
Ctrl-C does, and exits 124, as `timeout(1)` does. It is not a failure of the pipeline: one that
fails earlier exits 1, and one that ends earlier exits 0. Raise the time, or leave `--timeout`
out. [Stop after a time](cli.md#timeout) has the details.
