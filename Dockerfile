FROM ashwoods/gst-base:1.16.2

COPY . /src
WORKDIR /src
RUN set -ex \
    && pip install -r req-install.txt \
    && pip install -r req-test.txt \
    && pip install -e ./

