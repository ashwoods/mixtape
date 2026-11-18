# Fallback for running the checks without a native GStreamer: the Ubuntu packages
# of CI's `stock` job (.github/workflows/ci.yml), without the Rust plugins, running
# scripts/check. The docs' samples call curl, which the runners have.
FROM ubuntu:24.04

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/

RUN apt-get update -yq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -yq --no-install-recommends \
        curl \
        python3 \
        python3-gi \
        python3-gst-1.0 \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-nice \
        gstreamer1.0-tools \
    && rm -rf /var/lib/apt/lists/*

# Keep the venv out of /src, which docker-compose mounts from the host.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

WORKDIR /src
COPY . /src
CMD ["scripts/check"]
