FROM ashwoods/gst-base:1.16.2

WORKDIR /tmp
RUN apt-get update -yq \
    && apt-get install git meson build-essential -yq \
    && git clone https://github.com/ashwoods/gst-c-crash.git \
    && cd gst-c-crash \
    && meson build/ && ninja install -C build \
    && apt-get remove --purge git meson build-essential -yq \
    && apt-get -yq clean \
    && rm -rf /var/lib/apt/lists/* \
    && cd /tmp \
    && rm -rf /tmp/gst-c-crash

COPY . /src
WORKDIR /src
RUN set -ex \
    && pip install -r req-install.txt \
    && pip install -r req-test.txt \
    && pip install -e ./

