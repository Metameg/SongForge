"""Completion-webhook scheduling and delivery for the MusicGPT simulator (issue #11).

The create endpoint returns synchronously and schedules delivery here as a *detached*
``asyncio`` task tracked on ``app.state.pending_webhook_tasks`` — never blocking the response,
and awaitable deterministically in tests via ``wait_for_pending_webhooks``. The actual wait
goes through the injected ``sleep_fn`` seam, so tests assert timing without a real sleep.

Each fault shapes what gets delivered (or not):
- happy path / delayed / duplicate  -> COMPLETED, one (or two identical) ``music_ai`` webhooks
- url-expires-before-ingest         -> COMPLETED, webhook carries an already-expired audio URL
- webhook-never-arrives             -> COMPLETED on the API side, but no webhook is POSTed
- error / failed                    -> ERROR/FAILED status, webhook carries the failure status
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI

from songforge.logging_setup import get_logger
from songforge.simulator.config import SimulatorSettings
from songforge.simulator.faults import Fault
from songforge.simulator.schemas import ByIdStatus, WebhookPayload
from songforge.simulator.store import TaskRecord, TaskStore

log = get_logger(__name__)


def build_audio_url(settings: SimulatorSettings, token: str) -> str:
    """The public URL the ``/audio/{token}`` route serves for a minted token."""
    return f"{settings.public_base_url.rstrip('/')}/audio/{token}"


def resolve_delay(
    settings: SimulatorSettings, fault: Fault, delay_override: float | None
) -> float:
    """Pick the webhook delay: explicit header override > fault-specific > default."""
    if delay_override is not None:
        return delay_override
    if fault is Fault.DELAYED_WEBHOOK:
        return settings.delayed_webhook_delay_seconds
    return settings.webhook_delay_seconds


def schedule_webhook(app: FastAPI, record: TaskRecord, delay: float) -> None:
    """Fire-and-forget the completion webhook, tracked so tests can await it."""
    tasks: set[asyncio.Task[None]] = app.state.pending_webhook_tasks
    task = asyncio.create_task(_deliver(app, record, delay))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _deliver(app: FastAPI, record: TaskRecord, delay: float) -> None:
    sleep_fn = app.state.sleep_fn
    await sleep_fn(delay)

    store: TaskStore = app.state.store
    settings: SimulatorSettings = app.state.settings
    fault = record.fault

    if fault in (Fault.ERROR, Fault.FAILED):
        record.status = ByIdStatus.ERROR if fault is Fault.ERROR else ByIdStatus.FAILED
        payload = WebhookPayload(
            task_id=record.task_id,
            conversion_id=record.conversion_id_1,
            status=record.status.value,
        )
        await _post(app, record.webhook_url, payload)
        return

    # Generation "completed" on the provider side.
    expired = fault is Fault.URL_EXPIRES_BEFORE_INGEST
    token = store.mint_token(record.task_id, expired=expired)
    record.status = ByIdStatus.COMPLETED

    if fault is Fault.WEBHOOK_NEVER_ARRIVES:
        # Completed, but the delivery is "lost" — a later /byId poll would still find it.
        log.info("sim_webhook_suppressed", task_id=record.task_id, reason="never-arrives")
        return

    payload = WebhookPayload(
        task_id=record.task_id,
        conversion_id=record.conversion_id_1,
        conversion_path=build_audio_url(settings, token),
        conversion_duration=record.duration,
        title=record.title,
    )
    await _post(app, record.webhook_url, payload)
    if fault is Fault.DUPLICATE_WEBHOOK:
        # Real APIs retry delivery; the duplicate is byte-identical so consumers dedupe.
        await _post(app, record.webhook_url, payload)


async def _post(app: FastAPI, url: str, payload: WebhookPayload) -> None:
    client = app.state.http_client
    await client.post(url, json=payload.model_dump())
    log.info("sim_webhook_delivered", url=url, task_id=payload.task_id, status=payload.status)
