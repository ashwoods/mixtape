# Fallback for running the checks without a native GStreamer: the same
# Ubuntu packages as CI (.github/workflows/ci.yml), running scripts/check.
FROM ubuntu:24.04

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/

RUN apt-get update -yq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -yq --no-install-recommends \
        python3 \
        python3-gi \
        python3-gst-1.0 \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
    && rm -rf /var/lib/apt/lists/*

# Keep the venv out of /src, which docker-compose mounts from the host.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

WORKDIR /src
COPY . /src
CMD ["scripts/check"]
