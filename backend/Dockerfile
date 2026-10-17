# Backend image — shared by the migrate, web, and worker services (different commands).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency manifests first for layer caching.
COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

# Editable install keeps the package at /app/src so alembic.ini and migrations/
# resolve relative to /app at runtime (see songforge.boot._BACKEND_ROOT).
RUN pip install -e .

EXPOSE 8000

# Default command; compose overrides per service (songforge-boot / -web / -worker).
CMD ["songforge-web"]
