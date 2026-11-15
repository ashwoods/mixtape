"""The checks that keep a web page from reaching a player it should not control.

There is no authentication: the server binds to localhost by default, and these checks
stop other sites in the user's browser (cross-origin requests, DNS rebinding)."""

import ipaddress
import socket
from collections.abc import Mapping
from urllib.parse import urlsplit


def is_loopback(host: str) -> bool:
    """Whether a bind address only accepts connections from this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _hostname(host: str) -> str | None:
    try:
        return urlsplit(f"//{host}").hostname
    except ValueError:  # an unterminated [ipv6
        return None


def _allowed_host(name: str | None, *, loopback: bool) -> bool:
    """Whether a Host header's hostname names this server rather than a site that rebound
    its DNS name to it: `localhost` or an IP address (a loopback one when `loopback`), or,
    on other binds, this machine's own name."""
    if name is None:
        return False
    if is_loopback(name):
        return True
    if loopback:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        own = socket.gethostname().lower().removesuffix(".local")
        return name.removesuffix(".local") == own


def check_headers(headers: Mapping[str, str], *, loopback: bool) -> str | None:
    """Why a request must be refused (403), or None.

    The Host must name this machine (see `_allowed_host`), which defeats DNS rebinding.
    An Origin, which browsers send and curl does not, must be the Host itself."""
    host = headers.get("host", "")
    if not _allowed_host(_hostname(host), loopback=loopback):
        return f"Host {host!r} is not allowed"
    origin = headers.get("origin")
    if origin is not None and urlsplit(origin).netloc.lower() != host.lower():
        return f"Origin {origin!r} does not match Host {host!r}"
    return None
