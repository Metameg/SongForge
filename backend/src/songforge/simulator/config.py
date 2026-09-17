"""Env-driven configuration for the MusicGPT simulator (dev/test only).

Kept isolated from :mod:`songforge.config` per issue #11 — the simulator is a throwaway dev
fake, and its tunables (webhook delay, audio pool location, URL expiry) must never bloat the
main app's ``Settings`` or leak prod-facing knobs. Read via ``SIM_``-prefixed env vars; the
main app is otherwise unchanged (it already has ``musicgpt_base_url`` to point at this
service or the real API).
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Mapping
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

# Serializes the environment swap in `SimulatorSettings(_env=...)`, mirroring
# `songforge.config.Settings` so concurrent construction (e.g. parallel test workers)
# cannot race on the process-wide os.environ.
_env_swap_lock = threading.Lock()


class SimulatorSettings(BaseSettings):
    """Typed, validated tunables for the fault-injectable MusicGPT simulator.

    Construct with no arguments to read the real environment. Pass ``_env=<mapping>`` to
    load from an explicit mapping instead (used by tests for determinism).
    """

    model_config = SettingsConfigDict(
        env_prefix="SIM_",
        case_sensitive=False,
        extra="ignore",
        env_file=None,
    )

    # Default completion-webhook delay when a request doesn't override it via the
    # X-Sim-Delay-Seconds header (issue #11 acceptance: "delay configurable, default ~5s").
    webhook_delay_seconds: float = 5.0
    # Webhook delay applied to the `delayed-webhook` fault (deliberately longer than the
    # happy-path default so the "overdue" waiting window is exercised).
    delayed_webhook_delay_seconds: float = 30.0
    # How long a freshly (re)issued audio URL stays valid before it "expires". The
    # url-expires-before-ingest fault serves an already-expired URL until /byId refreshes it.
    url_expiry_seconds: float = 300.0
    # Base URL the simulator advertises for served audio (the `conversion_path` /byId
    # `audio_url`). Matches the create client's ``MUSICGPT_BASE_URL`` so a server-side
    # consumer (ingest worker) can download it; overridable via ``SIM_PUBLIC_BASE_URL``.
    public_base_url: str = "http://simulator:8080"
    # Local royalty-free pool the simulator serves fake `conversion_path` audio from.
    audio_pool_dir: str = "app/static/audios"
    log_level: str = "INFO"

    def __init__(self, _env: Mapping[str, str] | None = None, **data: Any) -> None:
        if _env is None:
            super().__init__(**data)
            return
        # Load *exclusively* from the provided mapping (deterministic for tests): swap the
        # process environment for the duration of construction, then restore it.
        with _env_swap_lock:
            saved = dict(os.environ)
            os.environ.clear()
            os.environ.update(_env)
            try:
                super().__init__(**data)
            finally:
                os.environ.clear()
                os.environ.update(saved)


@functools.lru_cache(maxsize=1)
def get_settings() -> SimulatorSettings:
    """Process-wide cached settings read from the real environment."""
    return SimulatorSettings()
