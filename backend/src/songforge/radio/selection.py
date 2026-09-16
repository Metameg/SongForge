"""Pure static-song selection: candidate set + recent set -> chosen id (criterion #2).

Kept free of I/O so it is unit-testable without Redis/Postgres, per the PRD testing
decision to keep the coordinator's decision logic on the fast unit path.
``songforge.radio.coordinator`` wires this to the real catalog (candidate ids) and the
Redis-backed recent-history store (recent ids).

Issue #8, phase 3 (green) implementation.
"""

from __future__ import annotations

import random


def pick_static(
    candidate_ids: list[str],
    recent_ids: set[str],
    *,
    current_id: str | None = None,
) -> str:
    """Choose the next static song id, avoiding ``recent_ids``; NEVER returns empty.

    Rule (spec "never-empty fallback"):
      1. Prefer a candidate in ``candidate_ids`` that is NOT in ``recent_ids``. When more
         than one qualifies, callers must not rely on a particular one being chosen
         beyond "some non-recent candidate" — tests assert membership, not identity,
         except where only one non-recent candidate exists.
      2. If every candidate is recent, or ``candidate_ids`` has exactly one element, the
         station must still not go silent: replay ``current_id`` if it is not None and a
         member of ``candidate_ids``, else fall back to ``candidate_ids[0]``.

    Raises:
        ValueError: ``candidate_ids`` is empty — there is nothing to ever play. Callers
            (the coordinator) handle the "no songs in the catalog at all" idle case
            before reaching this function.
    """
    if not candidate_ids:
        raise ValueError("candidate_ids must not be empty")

    non_recent = [c for c in candidate_ids if c not in recent_ids]
    if non_recent:
        return random.choice(non_recent)

    if current_id is not None and current_id in candidate_ids:
        return current_id
    return candidate_ids[0]
