"""Data-contract audit (issue #13, phase 4): proves the real MusicGPT-simulator webhook
*emitter* (`simulator/delivery.py` + `simulator/schemas.py::WebhookPayload`) and the real
`/api/generation/webhook` *consumer* (`web/routes/webhook.py`) actually agree, end to end,
without either side's shape coming from a hand-authored test fixture.

Why this file exists: `tests/test_webhook_route.py` proves the route's own logic (its
`_webhook_body()` helper hand-builds a dict "mirroring" `WebhookPayload`'s fields by eye --
a real drift between the simulator's emitted body and that hand-typed dict would go
unnoticed, green on both sides). `tests/test_webhook_ingest_e2e_integration.py` already
wires the *real* simulator to the *real* route the same way this file does, but it is
`@pytest.mark.integration` (skipped without a live Postgres) -- so on a normal `pytest -q
-m "not integration"` run (this repo's everyday/CI default), NOTHING actually drives real
simulator output through the real route. This file closes that gap with an in-memory
SQLite session (like `test_webhook_route.py`), so the contract is checked on every run,
not just when Postgres happens to be up.

No hand-crafted webhook body appears anywhere below -- every payload the route receives
was produced by `songforge.simulator.delivery` from a real `POST /api/public/v1/MusicAI`
call, routed in-process via `httpx.ASGITransport` (no real sockets, no real time: the
simulator's `sleep_fn` is swapped for `make_recording_sleep`).
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import get_settings
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Base,
    Job,
)
from songforge.simulator.app import create_app as create_sim_app
from songforge.simulator.app import wait_for_pending_webhooks
from songforge.simulator.faults import FAULT_HEADER, Fault
from songforge.web.app import create_app as create_web_app
from songforge.web.routes.webhook import get_notify_dependency, get_session
from tests.simulator_helpers import CREATE_PATH, DEFAULT_BODY, make_recording_sleep

_seq_counter = itertools.count(1)


def _assign_test_seq(mapper: Any, connection: Any, target: Job) -> None:
    if target.seq is None:
        target.seq = next(_seq_counter)


@pytest.fixture(autouse=True)
def _sqlite_seq_shim() -> Iterator[None]:
    """See `tests/test_create_route.py`'s module docstring: SQLite can't server-
    generate `Job.seq` (a Postgres `Identity`), so this fills it in for this file."""
    event.listen(Job, "before_insert", _assign_test_seq)
    yield
    event.remove(Job, "before_insert", _assign_test_seq)


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _NotifySpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, channel: str, payload: str) -> None:
        self.calls.append((channel, payload))


async def _get_job(sessionmaker: async_sessionmaker[AsyncSession], job_id: str) -> Job:
    async with sessionmaker() as session:
        return await session.get(Job, job_id)  # type: ignore[return-value]


async def _insert_waiting_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: str,
    *,
    task_id: str,
    conversion_id_1: str,
    conversion_id_2: str,
) -> None:
    async with sessionmaker() as session:
        session.add(
            Job(
                job_id=job_id,
                user_id="user-1",
                prompt=str(DEFAULT_BODY["prompt"]),
                state=JOB_STATE_WAITING_FOR_WEBHOOK,
                task_id=task_id,
                conversion_id_1=conversion_id_1,
                conversion_id_2=conversion_id_2,
                webhook_url="http://web.test/api/generation/webhook",
            )
        )
        await session.commit()


def _build_real_rig(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[httpx.AsyncClient, _NotifySpy]:
    """Wires the REAL simulator app's `http_client` seam directly to the REAL
    `/api/generation/webhook` route (an in-process `httpx.ASGITransport`, no real
    sockets) -- so `simulator/delivery.py`'s `_post` call IS the request the route
    handles. Only the DB session and the NOTIFY hook are swapped for test doubles
    (SQLite + a spy), exactly like `test_webhook_route.py`; the payload shape and its
    parsing are 100% production code on both ends."""
    web_app = create_web_app()
    notify_spy = _NotifySpy()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    web_app.dependency_overrides[get_session] = _override_session
    web_app.dependency_overrides[get_notify_dependency] = lambda: notify_spy

    webhook_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web.test"
    )
    return webhook_client, notify_spy


async def _create_and_deliver(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: str,
    *,
    fault: Fault = Fault.NONE,
) -> tuple[dict[str, Any], _NotifySpy]:
    """Drives a real `POST /api/public/v1/MusicAI` -> real webhook delivery -> real
    `/api/generation/webhook`, returning the real create-handles and the notify spy."""
    webhook_client, notify_spy = _build_real_rig(sessionmaker)
    sleep_fn, _delay_calls = make_recording_sleep()
    sim_app = create_sim_app(http_client=webhook_client, sleep_fn=sleep_fn)
    sim_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sim_app), base_url="http://sim.test"
    )
    if fault is not Fault.NONE:
        sim_client.headers[FAULT_HEADER] = fault.value

    async with sim_client:
        create_resp = await sim_client.post(
            CREATE_PATH,
            json={**DEFAULT_BODY, "webhook_url": "http://web.test/api/generation/webhook"},
        )
        assert create_resp.status_code == 200
        handles: dict[str, Any] = create_resp.json()

        await _insert_waiting_job(
            sessionmaker,
            job_id,
            task_id=handles["task_id"],
            conversion_id_1=handles["conversion_id_1"],
            conversion_id_2=handles["conversion_id_2"],
        )

        await wait_for_pending_webhooks(sim_app)

    await webhook_client.aclose()
    return handles, notify_spy


# ── Happy path: the real emitter's body is accepted and parsed correctly ──────────


async def test_real_simulator_webhook_transitions_job_to_ingest_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """No hand-crafted webhook body: `simulator/delivery.py` builds and POSTs the real
    `WebhookPayload`, and the real route must parse it and transition the job -- this
    is the structural check `test_webhook_route.py`'s hand-typed fixture cannot make."""
    handles, notify_spy = await _create_and_deliver(sessionmaker, "job-contract-happy")

    job = await _get_job(sessionmaker, "job-contract-happy")
    assert job.state == JOB_STATE_INGEST_PENDING
    assert job.audio_url is not None and job.audio_url.startswith("http")
    assert job.audio_duration is not None
    assert job.title is not None

    assert len(notify_spy.calls) == 1
    channel, payload = notify_spy.calls[0]
    assert channel == get_settings().ingest_channel
    assert payload == "job-contract-happy"
    assert handles["conversion_id_1"] != handles["conversion_id_2"]


# ── Failure-status fault: the real emitter's failure body is handled correctly ────


async def test_real_simulator_failed_webhook_marks_job_failed(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The simulator's `Fault.FAILED` delivers a real `WebhookPayload` with `status`
    set and `conversion_path=None` (`simulator/delivery.py`'s error/failed branch) --
    proves the route's failure detection (`body.status is not None`) matches what the
    real emitter actually sends on this path, not an invented status string."""
    _handles, notify_spy = await _create_and_deliver(
        sessionmaker, "job-contract-failed", fault=Fault.FAILED
    )

    job = await _get_job(sessionmaker, "job-contract-failed")
    assert job.state == JOB_STATE_FAILED
    assert notify_spy.calls == []


# ── Duplicate delivery: the real emitter's byte-identical retry is a no-op ────────


async def test_real_simulator_duplicate_webhook_does_not_double_notify(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`Fault.DUPLICATE_WEBHOOK` makes the real simulator POST the identical real
    payload twice (mirrors a real API's at-least-once delivery) -- the route's
    idempotency check must hold against the real double-delivery, not just a test that
    calls the route twice with a hand-typed dict."""
    _handles, notify_spy = await _create_and_deliver(
        sessionmaker, "job-contract-dup", fault=Fault.DUPLICATE_WEBHOOK
    )

    job = await _get_job(sessionmaker, "job-contract-dup")
    assert job.state == JOB_STATE_INGEST_PENDING
    assert len(notify_spy.calls) == 1  # the byte-identical second delivery did not re-notify
