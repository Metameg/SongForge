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


class GenerationClient(Protocol):
    """Port ``songforge.jobs.dispatch`` depends on; tests inject a stub/fake."""

    async def create(
        self, *, prompt: str, lyrics: str | None, webhook_url: str
    ) -> GenerationHandles:
        """Submit a generation request. Raises ``GenerationRateLimited``,
        ``GenerationRejected``, or ``GenerationTransientError`` on non-200 outcomes."""
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

        data = response.json()
        return GenerationHandles(
            task_id=data["task_id"],
            conversion_id_1=data["conversion_id_1"],
            conversion_id_2=data["conversion_id_2"],
            eta=data["eta"],
            credit_estimate=data["credit_estimate"],
        )
