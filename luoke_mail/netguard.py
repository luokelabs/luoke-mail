"""Fetching an address that someone else wrote -- a mail's List-Unsubscribe header, a feed link on a web page --
only when it is on the public internet.

Fetched as they are, such addresses can make this Mac send requests to itself (127.0.0.1: the mail page, the
hub's endpoint, other apps' local servers) or to the local network (a router, a NAS): places a page on the
internet can't reach by itself. So the host has to resolve to public addresses only, and every redirect is
checked again before it is followed.

The name is resolved once more when connecting, so a host that answers differently the second time (DNS
rebinding) is not caught here; over https the certificate still has to match the name, which a service on this
Mac or the local network can't show for someone else's domain. A proxy set in the system settings still works:
the check is on the address asked for, not on the proxy."""
from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

PUBLIC, NOT_PUBLIC, UNRESOLVED = "public", "not_public", "unresolved"

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")            # the old "IPv4-compatible" form, ::a.b.c.d
# Clash and Surge in fake-ip mode (common in China) make the system resolver answer every name with an address in
# 198.18.0.0/15 and connect to the real host by name themselves. Refusing this range would refuse every address for
# those users; it is set aside for benchmarking (RFC 2544), so no real device on this Mac or the local network is in
# it. What the name really resolves to is then the proxy's to know -- as with any proxy, this check can't see it.
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def public_ip(addr: str) -> bool:
    """On the public internet: not loopback, private, link-local (where cloud metadata lives), shared (CGNAT),
    multicast or reserved. IPv4 carried in IPv6 (mapped, NAT64, IPv4-compatible) counts as that IPv4 address; the
    fake-ip range counts as public (above)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in _NAT64 or ip in _V4_COMPATIBLE:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _FAKE_IP:
        return True
    return ip.is_global and not ip.is_multicast


def host_kind(host: str, port: Optional[int] = None) -> str:
    """PUBLIC when every address the host resolves to is public (an address written out is judged as it is),
    UNRESOLVED when the name doesn't resolve now, NOT_PUBLIC otherwise."""
    host = (host or "").strip("[]")
    if not host:
        return NOT_PUBLIC
    try:
        ipaddress.ip_address(host)
        return PUBLIC if public_ip(host) else NOT_PUBLIC
    except ValueError:
        pass
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(host, port or 443, 0, socket.SOCK_STREAM)}
    except (OSError, UnicodeError):
        return UNRESOLVED
    if not addrs:
        return UNRESOLVED
    return PUBLIC if all(public_ip(a) for a in addrs) else NOT_PUBLIC


def url_kind(url: str, schemes: tuple = ("http", "https")) -> str:
    """host_kind of an http(s) address; another scheme, or one that doesn't parse, is NOT_PUBLIC."""
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except ValueError:
        return NOT_PUBLIC
    if p.scheme not in schemes:
        return NOT_PUBLIC
    return host_kind(p.hostname or "", port or (443 if p.scheme == "https" else 80))


def public_host(host: str, port: Optional[int] = None) -> bool:
    """Every address the host resolves to is public. Can't resolve: no."""
    return host_kind(host, port) == PUBLIC


def public_url(url: str, schemes: tuple = ("http", "https")) -> bool:
    """An http(s) address whose host is on the public internet."""
    return url_kind(url, schemes) == PUBLIC


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an address `allow` takes; with no `allow`, none at all (the 3xx comes back as an
    HTTPError with that code)."""

    def __init__(self, allow: Optional[Callable[[str], bool]]):
        self.allow = allow

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.allow is None:
            return None
        if not self.allow(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect to an address that is not on the public internet",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(req, timeout: float, allow: Optional[Callable[[str], bool]] = None):
    """urllib's urlopen, with redirects followed only where `allow` says (none when it is None). The first address
    is the caller's to check."""
    return urllib.request.build_opener(_Redirects(allow)).open(req, timeout=timeout)
