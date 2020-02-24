# mixtape: awesome mix vol. 1 🎵📼 

![Pypi page] ![Pypi license] ![Pypi version]

|[●▪▪●]| A python based `gstreamer` **player**-mini-framework.

**This project is in pre-alpha and most functionality is incomplete and the API is
most likely to change**

[Gstreamer] is a pipeline based framework for building complex multimedia processing 
systems. It is written in C, based on [GObject], and offers several bindings in other languages,
including python.

If you are unfamiliar with Gstreamer, you should start with the [GStreamer tutorials]. 

`mixtape` offers a few utility classes and functions for a gstreamer `application`,
and auto generates service interfaces for your application.

## Project goal

Python is a great language for prototyping and integration tests. The goal of the project is 
to have something in between `gst-launch` and writing a full python application for testing
and prototyping.


*Features*:

* Pluggable declarative definitions of Player features.
* Optional loops (glib, asyncio, ...) (WIP)
* Auto-generated `cmdline`, `console`, `http` and `dbus` service interfaces* (WIP)
* `cmdline`, `console`, `http` and `dbus` service cmdline commands to jump start services 
  from pipeline descriptions (WIP)

## Quickstart

## Install

You can use `pip` to install `mixtape`:

    pip install mixtape

## Usage

You can use `mixtape` similar to how you use `gst-launch-1.0` by passing 
a `pipeline description` to the `from_desc` constructor:



```python
from mixtape import AsyncPlayer as Player


desc = "videotestsrc num-buffers=100 ! fakesink"

async def main(self):
    p = Player.from_desc(desc)
    await p.play()
    asyncio.sleep(5)
    await p.stop()


asyncio.run(main())

```

The run classmethod is a shortcut for setting up the asyncio boilerplate and 
running a pipeline until an eos event or error: 

```python
from mixtape import AsyncPlayer as Player

desc = "videotestsrc num-buffers=100 ! fakesink"
p = Player.from_desc(desc)  # creates a player from a pipeline description
p.run(autoplay=True)  # init of pipeline (i.e. bus) and sets the pipeline to playing state (default)
```

You can run a loop and the player in a background thread:

```python
from mixtape import AsyncPlayer as Player
import threading

desc = "videotestsrc ! fakesink"

p = player.from_description(desc)
t = threading.Thread(target=lambda: p.run(autoplay=False)) # init the player in another thread
t.daemon = True  # set the thread to background
t.start()
seq = ['play', 'pause', 'play', 'stop']
for step in seq:
    sleep(3)
    s = getattr(p, "call_%s" % step) # call_x schedules the coroutine
    s()  # set the pipeline to `play`, `pause`, `play` and `stop`
t.join()
```

Constructing a dynamic pipeline and passing it to the player:

```python
import Gst

pipe = Gst.Pipeline.new('dynamic')
src = Gst.ElementFactory.make('videotestsrc')
sink = Gst.ElementFactory.make('fakesink')
pipe.add(src, sink)
src.link(sink)

p = Player(pipeline=pipe)
```

(WIP)
This example uses a `feature` class that uses the *centricular* webrtc example:

```python
from mixtape import AsyncPlayer as Player
from mixtape.features import WebRTC

desc = "videotestsrc ! tee name=tee ! queue ! fakesink"
p = Player.from_desc(desc, features=[WebRTC])
p.run()
p.webrtc.set_peer(1231)
p.webrtc.attach(tee)
```

This example uses a `feature` class that uses the ridgerun `gst-shark` profiler:

```python
from mixtape import AsyncPlayer as Player
from mixtape.features import GstShark

desc = "videotestsrc num-buffers=100 ! tee name=tee ! queue ! fakesink"
p = Player.from_desc(desc, features=[GstShark])
p.gstshark.set_tracers('latency')
p.run()
```

## Player base interface (Draft)

(WIP)

    - Player.from_desc

    - player.run

    - player.play

    - player.stop

    - player.pause


## Feature interface (Draft)

(WIP)

    from mixtape.features import AwesomeFeature, Attachable


    class WebRTC(AwesomeFeature, Attachable):

        name = 'webrtc'
        plugins_dependencies = ['webrtcbin', 'nice']
        bin_desc = ""

    def check(self, pipeline):
        pass

    def attach(self, pipeline, element, pad):
        pass

    def detach(self, pipeline, element, pad):
        pass

    def check(self, pipeline):
        pass

    def init(self, pipeline)
        pass

    def exit(self, pipeline)
        pass

----

    ....___.___.___.___.___.__....
    |:                          :|
    |:    Awesome mix vol 1     :|
    |:     ,-.   _____   ,-.    :|
    |:    ( `)) [_____] ( `))   :|
    |:     `-`   ' ' '   `-`    :|
    |:     ,______________.     :|
    |...../::::o::::::o::::\.....|
    |..../:::O::::::::::O:::\....|
    |---/----.----.----.-----`---|

We are Groot.

[GStreamer]: https://gstreamer.freedesktop.org/
[GObject]: https://developer.gnome.org/gobject/stable/
[GStreamer tutorials]: https://gstreamer.freedesktop.org/documentation/tutorials/index.html
[Pypi page]: https://img.shields.io/pypi/v/mixtape.svg
[Pypi license]: https://img.shields.io/pypi/l/mixtape.svg
[Pypi version]: https://img.shields.io/pypi/pyversions/mixtape.svg