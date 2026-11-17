"""A WebRTC client: publishes a video source and plays another client's. Two of them make a call.

mixtape run examples.webrtc_client:Host --http 8000
mixtape run examples.webrtc_client:Client --http 8001
python3 examples/webrtc_client.py --help
"""

# --8<-- [start:publish]
import argparse
import asyncio
import logging
import os
import threading
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from mixtape import Player, command, prop
from mixtape.exceptions import CommandError, PlayerPipelineError

SERVER = "ws://127.0.0.1:8443"


def publisher(name: str, *, server: str = SERVER, serve: bool = False, camera: bool = False) -> str:
    """The pipeline description of a client: its source, sent by webrtcsink to whoever calls."""
    source = "autovideosrc" if camera else "videotestsrc is-live=true"
    sink = f'webrtcsink name=out meta="meta,name={name}" signaller::uri={server}'
    sink += " stun-server=NULL congestion-control=disabled"  # all on this machine
    if serve:  # this client also runs the signalling server
        url = urlsplit(server)
        sink += f" run-signalling-server=true signalling-server-host={url.hostname}"
        sink += f" signalling-server-port={url.port}"
    return f"{source} ! videoconvert ! videoscale ! video/x-raw,width=320,height=240 ! {sink}"


# --8<-- [end:publish]

logger = logging.getLogger(__name__)
STOP_TIMEOUT = 2  # seconds the listener gets to stop


# --8<-- [start:client]
class Client(Player):
    """Publishes its source and plays the video of the peer it calls."""

    description = publisher("client")
    sink = "fakesink sync=false"  # what the remote video goes to: autovideosink shows it

    def __init__(self, pipeline: Gst.Pipeline, **kwargs: Any) -> None:
        super().__init__(pipeline, **kwargs)
        self._signaller = pipeline.get_by_name("out").get_property("signaller")
        self._signaller.connect("notify::client-id", self._on_joined)
        self._offered: dict[str, str] = {}  # everyone who publishes, us too: id to name
        self._remote: str | None = None
        self._call: Gst.Bin | None = None
        self._webrtcbin: Gst.Element | None = None
        self._frames = 0
        self._listening = False
        # a listener of the same signalling server: it is told who publishes
        self._directory = type(self._signaller)(uri=self._signaller.props.uri)
        self._directory.set_property("role", "listener")
        self._directory.connect("producer-added", self._on_producer_added)
        self._directory.connect("producer-removed", self._on_producer_removed)
        self._directory.connect("error", lambda _, message: logger.warning(message))

    async def set_state(self, state: Gst.State) -> Gst.StateChangeReturn:
        if state == Gst.State.NULL:  # the call ends before the pipeline, the listener after it
            self.hangup()
        result = await super().set_state(state)
        if state == Gst.State.NULL and self._listening:
            await self._stop_listening()
        return result

    async def _stop_listening(self) -> None:
        """Stop the listener on a thread of its own, and wait at most a few seconds: the
        signaller's stop deadlocks when a peer's status arrives as it stops."""
        self._listening = False
        loop = asyncio.get_running_loop()
        stopped = loop.create_future()

        def stopped_now() -> None:
            if not stopped.done():  # not timed out
                stopped.set_result(None)

        def stop() -> None:
            self._directory.emit("stop")
            if not loop.is_closed():
                _ = loop.call_soon_threadsafe(stopped_now)

        threading.Thread(target=stop, daemon=True).start()
        try:
            await asyncio.wait_for(stopped, STOP_TIMEOUT)
        except TimeoutError:
            logger.warning("the listener did not stop in %ss", STOP_TIMEOUT)

    def _on_loop(self, callback: Any, *args: object) -> None:
        """Run `callback` on the asyncio loop: the signallers call from their own threads."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(callback, *args)

    def _on_joined(self, *_: object) -> None:
        self._on_loop(self._joined)

    def _joined(self) -> None:
        if not self._listening:  # the server is up: ask it who publishes
            self._listening = True
            self._directory.emit("start")
        self._offer_changed()

    def _on_producer_added(self, _: object, peer: str, meta: Gst.Structure, __: bool) -> None:
        self._on_loop(self._offer, peer, meta.get_string("name") or peer)

    def _on_producer_removed(self, _: object, peer: str, meta: Gst.Structure) -> None:
        self._on_loop(self._offer, peer, None)

    def _offer(self, peer: str, name: str | None) -> None:
        if name is None:
            self._offered.pop(peer, None)
            if peer == self._remote:
                self.hangup()  # the peer left
        else:
            self._offered[peer] = name
        self._offer_changed()

    def _offer_changed(self) -> None:
        self.notify("peer_id")
        self.notify("producers")

    # --8<-- [end:client]

    # --8<-- [start:props]
    @prop
    def name(self) -> str:
        """The name this client publishes under"""
        return self.pipeline.get_by_name("out").get_property("meta").get_string("name")

    @prop
    def peer_id(self) -> str | None:
        """This client's id on the signalling server, once it has joined"""
        return self._signaller.props.client_id

    @prop
    def producers(self) -> dict[str, str]:
        """The other clients that publish: id to name"""
        return {peer: name for peer, name in self._offered.items() if peer != self.peer_id}

    @prop
    def remote(self) -> str | None:
        """The id of the client whose video is playing"""
        return self._remote

    @prop
    def connection(self) -> str:
        """The WebRTC connection state of the call: new, connecting, connected, ..."""
        if self._webrtcbin is None:
            return "none"
        return self._webrtcbin.get_property("connection-state").value_nick

    @prop
    def frames(self) -> int:
        """The video frames received from the remote client so far"""
        return self._frames

    # --8<-- [end:props]

    # --8<-- [start:call]
    @command
    def call(self, peer: str | None = None) -> str:
        """Play a client's video: its id or name, or the first one that publishes."""
        if peer is None and not self.producers:
            raise CommandError("nobody else publishes")
        found = [p for p, name in self.producers.items() if peer in (None, p, name)]
        if not found:
            raise CommandError(f"no client {peer!r} publishes: {list(self.producers.values())}")
        self.hangup()
        self._remote = found[0]
        src = "webrtcsrc name=in stun-server=NULL"
        src += f" signaller::uri={self._signaller.props.uri} signaller::producer-peer-id={found[0]}"
        call = self._call = Gst.parse_bin_from_description(
            f"{src} ! videoconvert name=convert ! {self.sink} name=display", False
        )
        # both come on GStreamer threads: hand them to the loop, with the call they belong to
        call.get_by_name("in").get_property("signaller").connect(
            "webrtcbin-ready", lambda _, __, webrtcbin: self._on_loop(self._ready, call, webrtcbin)
        )
        call.get_by_name("convert").get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, lambda *_: self._on_frame(call)
        )
        self.pipeline.add(call)
        call.sync_state_with_parent()
        self.notify("remote")
        return found[0]

    @command
    def hangup(self) -> None:
        """Stop playing the remote video."""
        if self._call is None:
            return
        self.pipeline.remove(self._call)  # first: it may post an error while it stops
        self._call.set_state(Gst.State.NULL)
        self._call = self._remote = self._webrtcbin = None
        self._frames = 0
        for path in ("remote", "connection", "frames"):
            self.notify(path)

    def _ready(self, call: Gst.Bin, webrtcbin: Gst.Element) -> None:
        if call is not self._call:  # a call that ended meanwhile
            return
        self._webrtcbin = webrtcbin
        webrtcbin.connect(
            "notify::connection-state", lambda *_: self._on_loop(self.notify, "connection")
        )

    def _on_frame(self, call: Gst.Bin) -> Gst.PadProbeReturn:
        self._on_loop(self._count, call)
        return Gst.PadProbeReturn.OK

    def _count(self, call: Gst.Bin) -> None:
        if call is not self._call:
            return
        self._frames += 1
        if self._frames % 30 == 1:  # often enough for a control page
            self.notify("frames")

    # --8<-- [end:call]


# --8<-- [start:host]
class Host(Client):
    """A client that also runs the signalling server, the way a call starts."""

    description = publisher("host", serve=True)

    def __init__(self, pipeline: Gst.Pipeline, **kwargs: Any) -> None:
        os.environ.setdefault("WEBRTCSINK_SIGNALLING_SERVER_LOG", "error")  # not a line per peer
        super().__init__(pipeline, **kwargs)


# --8<-- [end:host]


# --8<-- [start:main]
TIMEOUT = 30  # seconds for a call to get going and show its frames


async def until(client: Client, done: Callable[[], object]) -> None:
    """Wait for `done()`, and raise the error if the pipeline fails meanwhile."""
    while not done():
        if client.error is not None:
            raise client.error
        await asyncio.sleep(0.1)


async def talk(client: Client, peer: str | None, frames: int) -> str:
    """Call `peer` once it publishes; return who it was when its video has `frames` frames."""

    def called() -> bool:
        try:
            client.call(peer)
        except CommandError:
            return False  # not there yet
        return True

    await until(client, called)
    remote = client.producers[client.remote or ""]
    await until(client, lambda: client.frames >= frames)
    return f"{client.name} received {frames} frames from {remote}"


async def demo(server: str, frames: int) -> None:
    """A call on one machine: alice runs the signalling server, and she and bob call each other."""
    async with (
        Host.from_description(publisher("alice", server=server, serve=True)) as alice,
        Client.from_description(publisher("bob", server=server)) as bob,
    ):
        await alice.play()
        await until(alice, lambda: alice.peer_id)  # the server is listening
        await bob.play()
        for line in await asyncio.gather(talk(alice, "bob", frames), talk(bob, "alice", frames)):
            print(line)
        alice.hangup()  # both, before either one stops publishing
        bob.hangup()


async def run(args: argparse.Namespace) -> None:
    if args.demo:
        async with asyncio.timeout(TIMEOUT):
            await demo(args.server, args.frames)
        return
    description = publisher(args.name, server=args.server, serve=args.serve, camera=args.camera)
    async with (Host if args.serve else Client).from_description(description) as client:
        client.sink = args.sink
        await client.play()
        if args.call is None:
            await asyncio.Event().wait()  # publish until interrupted
        async with asyncio.timeout(TIMEOUT):
            print(await talk(client, args.call or None, args.frames))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a video source and call another client.")
    parser.add_argument("--name", default="client", help="the name to publish under")
    parser.add_argument(
        "--server", default=SERVER, help="the signalling server (default: %(default)s)"
    )
    parser.add_argument("--serve", action="store_true", help="also run the signalling server")
    parser.add_argument(
        "--camera", action="store_true", help="publish the camera, not a test pattern"
    )
    parser.add_argument(
        "--sink", default=Client.sink, help="where the remote video goes (default: %(default)s)"
    )
    parser.add_argument(
        "--call",
        nargs="?",
        const="",
        metavar="PEER",
        help="call a client by id or name, or the first that publishes, and exit once its "
        "video has --frames frames",
    )
    parser.add_argument(
        "--frames", type=int, default=60, help="frames to wait for (default: %(default)s)"
    )
    parser.add_argument(
        "--demo", action="store_true", help="run alice and bob in this process, calling each other"
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
    except TimeoutError:
        print(f"error: no call within {TIMEOUT} s")
        return 1
    except PlayerPipelineError as exc:
        print(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# --8<-- [end:main]
