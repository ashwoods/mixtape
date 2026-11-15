"""The checks that keep a web page from reaching a player it should not control.

There is no authentication: the server binds to localhost by default, and these checks
stop other sites in the user's browser (cross-origin requests, DNS rebinding)."""

import ipaddress
from collections.abc import Mapping
from urllib.parse import urlsplit

LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


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


def check_headers(headers: Mapping[str, str], *, loopback: bool) -> str | None:
    """Why a request must be refused (403), or None.

    When `loopback` (the server is bound to a loopback address), the Host must name this
    machine, which defeats DNS rebinding. An Origin, which browsers send and curl does not,
    must be the Host itself."""
    host = headers.get("host", "")
    if loopback and _hostname(host) not in LOOPBACK_HOSTNAMES:
        return f"Host {host!r} is not allowed"
    origin = headers.get("origin")
    if origin is not None and urlsplit(origin).netloc.lower() != host.lower():
        return f"Origin {origin!r} does not match Host {host!r}"
    return None
