FROM debian:sid

# https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=863199
RUN mkdir -p /usr/share/man/man1

RUN set -ex \
    && apt-get -yq update \
    && apt-get install -yq --no-upgrade \
        gtk-doc-tools \
        libgstreamer1.0-dev \
        python3 \ 
        python3-pip \
        python3-gst-1.0 \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-tools \
        gstreamer1.0-nice \
        gstreamer1.0-libav \
        gstreamer1.0-python3-plugin-loader \
        libgstreamer-plugins-base1.0 \
        libgstreamer1.0-0 \
        gir1.2-gst-*

COPY . /src
RUN pip3 install -e /src[test]
WORKDIR /src

RUN groupadd -g 999 mixtape && \
    useradd -m -u 999 -g mixtape mixtape
# #iUSER mixtapei