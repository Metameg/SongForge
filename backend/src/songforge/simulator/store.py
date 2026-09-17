"""In-memory task store for the MusicGPT simulator (issue #11).

The seam every stateful behavior keys off: create records a :class:`TaskRecord`, the delayed
webhook flips its status and mints an audio token, and ``/byId`` reads the status back and
mints a *fresh* token (the URL-refresh mechanism). One store instance lives per app on
``app.state.store`` (not a module global) so each ``create_app()`` — and therefore each test
— is fully isolated.

Audio tokens are opaque handles the ``/audio/{token}`` route serves bytes for; a token
carries an ``expired`` flag so the ``url-expires-before-ingest`` fault can hand out a stale
token (served 403) until ``/byId`` reissues a valid one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from songforge.simulator.faults import Fault
from songforge.simulator.schemas import ByIdStatus


@dataclass
class TaskRecord:
    """The simulator's record of one in-flight/finished generation."""

    task_id: str
    conversion_id_1: str
    conversion_id_2: str
    prompt: str
    webhook_url: str
    fault: Fault
    duration: float
    title: str
    status: ByIdStatus = ByIdStatus.IN_QUEUE
    current_audio_token: str | None = None


class TaskStore:
    """Per-app registry of tasks plus the audio tokens they've issued."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        # token -> is-expired. `url-expires-before-ingest` stores an expired token; every
        # other completion (and every /byId refresh) stores a valid one.
        self._token_expired: dict[str, bool] = {}

    def add(self, record: TaskRecord) -> None:
        self._tasks[record.task_id] = record

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def mint_token(self, task_id: str, *, expired: bool = False) -> str:
        """Issue a fresh audio token for a task and record it as the current one."""
        token = uuid.uuid4().hex
        self._token_expired[token] = expired
        record = self._tasks.get(task_id)
        if record is not None:
            record.current_audio_token = token
        return token

    def has_token(self, token: str) -> bool:
        return token in self._token_expired

    def is_expired(self, token: str) -> bool:
        """True if the token is expired. Unknown tokens are treated as expired."""
        return self._token_expired.get(token, True)
