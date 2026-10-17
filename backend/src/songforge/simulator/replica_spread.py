"""Spread completion webhooks across every replica behind the webhook hostname.

A long-lived ``httpx.AsyncClient`` connects to the first address DNS returns and reuses
that connection, so every webhook lands on one ``web`` replica. Rewriting the URL to a
randomly chosen resolved address per delivery spreads them evenly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import socket
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

# (family, address) pairs for a hostname.
ResolveFn = Callable[[str, int], Awaitable[list[tuple[int, str]]]]

_CACHE_SECONDS = 5.0


async def _system_resolve(host: str, port: int) -> list[tuple[int, str]]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [(family, str(sockaddr[0])) for family, _type, _proto, _canon, sockaddr in infos]


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class ReplicaUrlPicker:
    def __init__(
        self,
        resolve: ResolveFn = _system_resolve,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._resolve = resolve
        self._clock = clock
        self._rand = rand
        self._cache: dict[str, tuple[list[str], float]] = {}

    async def pick(self, url: str) -> str:
        parts = urlsplit(url)
        host = parts.hostname
        # https needs the real hostname for TLS; IP literals and localhost have nothing to spread.
        if parts.scheme != "http" or host is None or host == "localhost" or _is_ip(host):
            return url
        addresses = await self._addresses(host, parts.port or 80)
        if not addresses:
            return url
        ip = addresses[int(self._rand() * len(addresses))]
        netloc = f"[{ip}]" if ":" in ip else ip
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit(parts._replace(netloc=netloc))

    async def _addresses(self, host: str, port: int) -> list[str]:
        cached = self._cache.get(host)
        if cached is not None and cached[1] > self._clock():
            return cached[0]
        try:
            infos = await self._resolve(host, port)
        except Exception:  # noqa: BLE001 - fall back to the original URL
            infos = []
        # The receiving web app binds 0.0.0.0, so prefer IPv4 whenever any exists.
        v4 = [addr for family, addr in infos if family == socket.AF_INET]
        chosen = v4 or [addr for _family, addr in infos]
        addresses = list(dict.fromkeys(chosen))
        self._cache[host] = (addresses, self._clock() + _CACHE_SECONDS)
        return addresses
