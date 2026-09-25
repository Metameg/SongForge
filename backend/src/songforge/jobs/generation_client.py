"""Generation API client — the dispatch-only seam to MusicGPT / the simulator (#12).

Ports ``app/home/services.py::MusicAPIClient.create_music`` (the old Flask app) onto
``httpx`` and this repo's typed conventions, targeting the fault-injectable simulator's
``POST /api/public/v1/MusicAI`` (issue #11: ``simulator/routes/create.py`` +
``simulator/schemas.py``) — swapping the real MusicGPT API in is a base-URL change only.

Used ONLY by ``songforge.jobs.dispatch``. ``POST /create`` (issue #12 criterion #1) must
never import or call this module — the job is persisted QUEUED before any external
call; dispatch alone talks to the generation API. ``tests/test_create_route.py``
monkeypatches ``HttpGenerationClient.create`` to explode if invoked, guarding that
boundary from regressing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from songforge.config import Settings
from songforge.logging_setup import get_logger

log = get_logger(__name__)


class GenerationRateLimited(Exception):
    """The generation API returned 429 (PRD #18: the authoritative rate-limit backstop
    over the best-effort Redis semaphore). A clean, retriable rejection: no charge, no
    handle issued. See ``.orchestrator/CONTEXT.md`` "In-scope decision: 429 handling"."""


class GenerationRejected(Exception):
    """A terminal 4xx (bad prompt, insufficient credits, ...) — not retriable."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"generation rejected: {status_code} {detail}")
        self.status_code = status_code
        self.detail = detail


class GenerationTransientError(Exception):
    """A 5xx response or a network timeout — retriable; full recovery beyond one
    dispatch-owned requeue is the (out-of-scope, later) watchdog's job."""


@dataclass(frozen=True)
class GenerationHandles:
    """The synchronous 200 response from the create call (criterion #4)."""

    task_id: str
    conversion_id_1: str
    conversion_id_2: str
    eta: int
    credit_estimate: float


# `/byId` status values (mirrors the simulator's `ByIdStatus`, `simulator/schemas.py`
# -- kept as plain strings here rather than importing that enum, matching this
# module's existing convention of not depending on the simulator's own types).
GENERATION_STATUS_IN_QUEUE = "IN_QUEUE"
GENERATION_STATUS_COMPLETED = "COMPLETED"
GENERATION_STATUS_ERROR = "ERROR"
GENERATION_STATUS_FAILED = "FAILED"


@dataclass(frozen=True)
class GenerationStatus:
    """Status-bearing outcome of a `/byId` poll (issue #16).

    Unlike `get_audio_url_by_id` (which raises unless the task is COMPLETED), the
    watchdog's `sweep_waiting_overdue` needs the raw STATUS to branch: COMPLETED ->
    recover (no re-charge), ERROR/FAILED -> terminal, IN_QUEUE -> leave waiting.
    `audio_url`/`duration`/`title` are only populated when `status == COMPLETED`.
    """

    status: str
    audio_url: str | None
    duration: float | None
    title: str | None


class GenerationClient(Protocol):
    """Port ``songforge.jobs.dispatch`` (and ``songforge.jobs.ingest``) depend on;
    tests inject a stub/fake."""

    async def create(
        self, *, prompt: str, lyrics: str | None, webhook_url: str
    ) -> GenerationHandles:
        """Submit a generation request. Raises ``GenerationRateLimited``,
        ``GenerationRejected``, or ``GenerationTransientError`` on non-200 outcomes."""
        ...

    async def get_audio_url_by_id(self, task_id: str) -> str:
        """By-handle URL refresh (issue #13, acceptance criterion #4): the webhook's
        ``conversion_path`` is only a *hint* that can expire before ingest runs; this
        re-fetches a fresh, unexpired audio URL for the same task. Raises
        ``GenerationRateLimited``, ``GenerationRejected`` (a 404 unknown ``task_id``
        included), or ``GenerationTransientError`` on non-200 outcomes -- the same
        typed-exception mapping as ``create`` -- and also raises
        ``GenerationTransientError`` if a 200 response has no usable ``audio_url``
        (the task isn't completed yet, or a malformed body)."""
        ...

    async def get_status_by_id(self, task_id: str) -> GenerationStatus:
        """By-handle STATUS lookup (issue #16, watchdog's ``sweep_waiting_overdue``):
        returns the raw status so the caller can branch (``COMPLETED`` -> recover,
        ``ERROR``/``FAILED`` -> terminal, ``IN_QUEUE`` -> leave waiting) instead of
        raising on anything but completion. Same typed-exception mapping as
        ``create``/``get_audio_url_by_id`` for the non-2xx/malformed-body paths."""
        ...


class HttpGenerationClient:
    """Real client: ``httpx`` POST to ``{musicgpt_base_url}/api/public/v1/MusicAI``.

    Ports the old Flask ``MusicAPIClient.create_music`` request shape (payload:
    ``prompt``/``lyrics``/``make_instrumental``/``vocal_only``/``webhook_url``; header:
    ``Authorization: {musicgpt_api_key}``) onto an async ``httpx`` call, discarding the
    old client's ``requests``/OpenAI-lyrics coupling.
    """

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http_client = http_client

    async def create(
        self, *, prompt: str, lyrics: str | None, webhook_url: str
    ) -> GenerationHandles:
        payload = {
            "prompt": prompt,
            "lyrics": lyrics or "",
            "make_instrumental": False,
            "vocal_only": False,
            "webhook_url": webhook_url,
        }
        headers = {"Authorization": self._settings.musicgpt_api_key}
        url = f"{self._settings.musicgpt_base_url}/api/public/v1/MusicAI"
        try:
            response = await self._http_client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise GenerationTransientError(f"generation API timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise GenerationTransientError(
                f"generation API request failed: {exc}"
            ) from exc

        if response.status_code == 429:
            raise GenerationRateLimited("generation API is at capacity")
        if response.status_code >= 500:
            raise GenerationTransientError(
                f"generation API returned {response.status_code}"
            )
        if response.status_code >= 400:
            raise GenerationRejected(response.status_code, response.text)

        # A malformed/short-lived-API-bug 200 (invalid JSON, or valid JSON missing an
        # expected field) is a retriable condition, not a crash: dispatch's catch-all
        # would otherwise have to treat it as truly unexpected (quality report HIGH
        # finding). Mapping it to the same typed exception the 5xx/timeout branch
        # raises keeps this the single place that decides "malformed 200 == retry".
        try:
            data = response.json()
            return GenerationHandles(
                task_id=data["task_id"],
                conversion_id_1=data["conversion_id_1"],
                conversion_id_2=data["conversion_id_2"],
                eta=data["eta"],
                credit_estimate=data["credit_estimate"],
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise GenerationTransientError(
                f"generation API returned a malformed 200 body: {exc}"
            ) from exc

    async def get_audio_url_by_id(self, task_id: str) -> str:
        status = await self.get_status_by_id(task_id)
        if status.status != GENERATION_STATUS_COMPLETED or not status.audio_url:
            # Missing/empty `audio_url` on a 200 -- either the task genuinely isn't
            # completed yet, or a malformed body; both are retriable from the
            # caller's perspective (ingest.py's refresh-and-retry), not a crash.
            raise GenerationTransientError(
                "by-id lookup returned no usable audio_url"
            )
        return status.audio_url

    async def get_status_by_id(self, task_id: str) -> GenerationStatus:
        url = f"{self._settings.musicgpt_base_url}/byId"
        headers = {"Authorization": self._settings.musicgpt_api_key}
        try:
            response = await self._http_client.get(
                url, params={"task_id": task_id}, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise GenerationTransientError(f"by-id lookup timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise GenerationTransientError(
                f"by-id lookup request failed: {exc}"
            ) from exc

        if response.status_code == 429:
            raise GenerationRateLimited("by-id lookup is at capacity")
        if response.status_code >= 500:
            raise GenerationTransientError(
                f"by-id lookup returned {response.status_code}"
            )
        if response.status_code >= 400:
            raise GenerationRejected(response.status_code, response.text)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise GenerationTransientError(
                f"by-id lookup returned a malformed 200 body: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise GenerationTransientError(
                "by-id lookup returned a malformed 200 body: not a JSON object"
            )

        return GenerationStatus(
            status=str(data.get("status") or ""),
            audio_url=data.get("audio_url"),
            duration=data.get("conversion_duration"),
            title=data.get("title"),
        )
