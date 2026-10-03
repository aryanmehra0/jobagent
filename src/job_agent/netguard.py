"""Fetch third-party URLs without letting them reach this machine's own network.

Job URLs and company websites come from scraped boards and feeds, not from the operator, so
they are untrusted. A hostile or compromised listing could point one at `http://127.0.0.1:8765`
(this dashboard), a router, or a cloud metadata address. Checking the first URL is not enough:
a public page can answer with a redirect to an internal one, and `requests` follows redirects
on its own. `safe_get` therefore turns automatic redirects off and re-checks every hop.

Limit, stated plainly: the hostname is resolved here and again by `requests`, so a DNS record
that changes between the two (rebinding) is not caught. Closing that needs the connection
pinned to the vetted address, which `requests` does not offer without a custom adapter.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

MAX_REDIRECTS = 4
_REDIRECT_CODES = (301, 302, 303, 307, 308)


def resolves_to_public_address(hostname: str, port: int) -> bool:
    """Whether every address the hostname resolves to is a public, routable one."""
    try:
        addresses = socket.getaddrinfo(hostname, port)
    except OSError:
        return False
    try:
        return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except ValueError:
        return False


def is_public_url(url: str, check: Callable[[str, int], bool] = resolves_to_public_address) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return check(parsed.hostname, port)


def safe_get(session, url: str, *, check: Callable[[str, int], bool] = resolves_to_public_address,
             max_redirects: int = MAX_REDIRECTS, **kwargs):
    """GET `url`, following redirects only while every hop is a public host.

    Returns the final response (streamed if `stream=True` was passed), or None when the
    start URL or any redirect target is not public or the chain is too long.
    """
    kwargs.pop("allow_redirects", None)
    current = url
    for _ in range(max_redirects + 1):
        if not is_public_url(current, check):
            return None
        response = session.get(current, allow_redirects=False, **kwargs)
        status = getattr(response, "status_code", 200)
        location: Optional[str] = response.headers.get("Location") if status in _REDIRECT_CODES else None
        if not location:
            return response
        response.close()
        current = urljoin(current, location)
    return None
