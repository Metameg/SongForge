"""``radio_state`` is a singleton — only ``id = 1`` is a valid row (issue #8, criterion #1).

The coordinator only ever reads/writes the fixed ``RADIO_STATE_SINGLETON_ID`` row, so a
stray second row (from a bug or a manual insert) would be silently ignored by
``session.get(RadioState, 1)`` while quietly corrupting the "single pointer of record"
invariant. A DB-level ``CheckConstraint("id = 1")`` makes the invariant real. SQLite (the
unit-test engine) enforces CHECK constraints, so this is testable without Postgres.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from songforge.models import RADIO_STATE_SINGLETON_ID, Base, RadioState


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_singleton_row_at_id_1_is_allowed(
    sessionmaker: async_sessionmaker[object],
) -> None:
    async with sessionmaker() as session:
        session.add(RadioState(id=RADIO_STATE_SINGLETON_ID, version=0))
        await session.commit()  # must not raise


async def test_a_second_pointer_row_is_rejected(
    sessionmaker: async_sessionmaker[object],
) -> None:
    async with sessionmaker() as session:
        session.add(RadioState(id=2, version=0))
        with pytest.raises(IntegrityError):
            await session.commit()
