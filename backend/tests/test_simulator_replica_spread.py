"""Webhook delivery is spread across every replica behind the webhook hostname."""

from __future__ import annotations

import socket

import httpx

from songforge.simulator.app import create_app
from songforge.simulator.replica_spread import ReplicaUrlPicker

URL = "http://web.railway.internal:8000/api/generation/webhook"


def _resolver(*pairs: tuple[int, str]):  # type: ignore[no-untyped-def]
    calls: list[str] = []

    async def resolve(host: str, port: int) -> list[tuple[int, str]]:
        calls.append(host)
        return list(pairs)

    return resolve, calls


async def test_spreads_successive_deliveries_across_every_replica() -> None:
    resolve, _ = _resolver(
        (socket.AF_INET, "10.0.0.1"), (socket.AF_INET, "10.0.0.2"), (socket.AF_INET, "10.0.0.3")
    )
    rolls = iter([0.0, 0.4, 0.9])
    picker = ReplicaUrlPicker(resolve, clock=lambda: 0.0, rand=lambda: next(rolls))

    urls = [await picker.pick(URL) for _ in range(3)]

    assert urls == [
        "http://10.0.0.1:8000/api/generation/webhook",
        "http://10.0.0.2:8000/api/generation/webhook",
        "http://10.0.0.3:8000/api/generation/webhook",
    ]


async def test_prefers_ipv4_and_dedupes_per_socket_type_duplicates() -> None:
    resolve, _ = _resolver(
        (socket.AF_INET6, "fd12::1"), (socket.AF_INET, "10.0.0.7"), (socket.AF_INET, "10.0.0.7")
    )
    picker = ReplicaUrlPicker(resolve, clock=lambda: 0.0, rand=lambda: 0.99)

    assert await picker.pick(URL) == "http://10.0.0.7:8000/api/generation/webhook"


async def test_brackets_ipv6_when_no_ipv4_exists() -> None:
    resolve, _ = _resolver((socket.AF_INET6, "fd12::1"))
    picker = ReplicaUrlPicker(resolve, clock=lambda: 0.0, rand=lambda: 0.0)

    assert await picker.pick(URL) == "http://[fd12::1]:8000/api/generation/webhook"


async def test_falls_back_to_the_original_url_when_resolution_fails() -> None:
    async def broken(host: str, port: int) -> list[tuple[int, str]]:
        raise OSError("Name or service not known")

    picker = ReplicaUrlPicker(broken, clock=lambda: 0.0, rand=lambda: 0.0)

    assert await picker.pick(URL) == URL


async def test_caches_the_address_list_briefly() -> None:
    resolve, calls = _resolver((socket.AF_INET, "10.0.0.1"))
    now = [0.0]
    picker = ReplicaUrlPicker(resolve, clock=lambda: now[0], rand=lambda: 0.0)

    await picker.pick(URL)
    await picker.pick(URL)
    assert len(calls) == 1
    now[0] = 5.1
    await picker.pick(URL)
    assert len(calls) == 2


async def test_leaves_localhost_ip_literals_and_https_untouched() -> None:
    resolve, calls = _resolver((socket.AF_INET, "10.0.0.1"))
    picker = ReplicaUrlPicker(resolve, clock=lambda: 0.0, rand=lambda: 0.0)

    for url in (
        "http://localhost:8000/hook",
        "http://127.0.0.1:8000/hook",
        "https://receiver.example/hook",
    ):
        assert await picker.pick(url) == url
    assert calls == []


async def test_injected_test_client_skips_address_rewriting() -> None:
    async with httpx.AsyncClient() as client:
        app = create_app(http_client=client)
        assert app.state.webhook_url_picker is None
