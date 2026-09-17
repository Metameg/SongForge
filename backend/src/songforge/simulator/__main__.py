"""Run the simulator with uvicorn: ``python -m songforge.simulator`` (or ``songforge-simulator``).

Dev/test only — never deployed to prod. Prod points ``MUSICGPT_BASE_URL`` at the real MusicGPT
API, so this service is simply not started there.
"""

from __future__ import annotations

import uvicorn

from songforge.simulator.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "songforge.simulator.app:app",
        host="0.0.0.0",  # noqa: S104 - containerised service binds all interfaces
        port=8080,
        log_config=None,  # structlog owns logging; don't let uvicorn reconfigure it
        access_log=False,  # match the web service's structured-log convention
    )
    _ = settings


if __name__ == "__main__":
    main()
