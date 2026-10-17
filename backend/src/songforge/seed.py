"""Seed the static library so the station always has content to play (spec #55, #76).

The static library is the set of curated ``*.mp3`` files under the configured
static-library directory (bind-mounted locally, an object-storage prefix in prod). Seeding
is **idempotent** — safe to run on every boot — keyed on the song id (the file stem):

    discover_static_tracks(dir) → upload_static_audio(storage) → seed_static_library(db)

Discovery/upload/row-insert are separated so the pure row-seed can be unit-tested against
in-memory SQLite with no filesystem or object store.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.logging_setup import get_logger
from songforge.models import SOURCE_STATIC, Song
from songforge.storage import ObjectStorage, audio_key

log = get_logger(__name__)


@dataclass(frozen=True)
class StaticTrack:
    """A single curated static-library track and where its audio lives."""

    id: str
    title: str
    object_key: str
    duration_seconds: int | None
    source_path: Path | None = None


def _prettify(stem: str) -> str:
    """Human-ish title from a filename stem; UUID-like stems get a generic label."""
    cleaned = stem.replace("_", " ").replace("-", " ").strip()
    compact = cleaned.replace(" ", "")
    if len(compact) >= 24 and all(c in "0123456789abcdefABCDEF" for c in compact):
        return f"Static Track {stem[:8]}"
    return cleaned.title()


def _read_duration_seconds(path: Path) -> int | None:
    """Best-effort duration via mutagen; None if it cannot be read."""
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path)
        if audio is not None and audio.info is not None:
            return int(audio.info.length)
    except Exception as exc:  # noqa: BLE001 - duration is non-critical metadata
        log.warning("static_duration_unreadable", path=str(path), error=str(exc))
    return None


def discover_static_tracks(audio_dir: str | Path) -> list[StaticTrack]:
    """Scan ``audio_dir`` for ``*.mp3`` and build a deterministic track list."""
    directory = Path(audio_dir)
    if not directory.is_dir():
        log.warning("static_library_dir_missing", dir=str(directory))
        return []
    tracks: list[StaticTrack] = []
    for path in sorted(directory.glob("*.mp3")):
        stem = path.stem
        tracks.append(
            StaticTrack(
                id=stem,
                title=_prettify(stem),
                object_key=audio_key(stem),
                duration_seconds=_read_duration_seconds(path),
                source_path=path,
            )
        )
    return tracks


def upload_static_audio(storage: ObjectStorage, tracks: list[StaticTrack]) -> int:
    """Upload any track whose audio object is missing from storage. Idempotent."""
    storage.ensure_bucket()
    uploaded = 0
    for track in tracks:
        if track.source_path is None or storage.exists(track.object_key):
            continue
        storage.put(track.object_key, track.source_path.read_bytes())
        uploaded += 1
    log.info("static_audio_uploaded", count=uploaded, total=len(tracks))
    return uploaded


async def seed_static_library(session: AsyncSession, tracks: list[StaticTrack]) -> int:
    """Insert catalog rows for any not-yet-present static track. Returns count inserted."""
    existing = set(
        (await session.execute(select(Song.id))).scalars().all()
    )
    inserted = 0
    for track in tracks:
        if track.id in existing:
            continue
        session.add(
            Song(
                id=track.id,
                title=track.title,
                source=SOURCE_STATIC,
                object_key=track.object_key,
                duration_seconds=track.duration_seconds,
            )
        )
        inserted += 1
    await session.commit()
    log.info("static_library_seeded", inserted=inserted, total=len(tracks))
    return inserted
