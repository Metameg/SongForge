"""Run the web app with uvicorn: ``python -m songforge.web`` (or ``songforge-web``)."""

from __future__ import annotations

import uvicorn

from songforge.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "songforge.web.app:app",
        host="0.0.0.0",  # noqa: S104 - containerised service binds all interfaces
        port=8000,
        log_config=None,  # structlog owns logging; don't let uvicorn reconfigure it
        access_log=False,  # our middleware emits the structured JSON access log instead
    )
    _ = settings


if __name__ == "__main__":
    main()
