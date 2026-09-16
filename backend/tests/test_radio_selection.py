"""Pure static-song selection logic (issue #8, criterion #2).

No I/O: direct unit tests of `pick_static`'s candidate-set + recent-set -> chosen-id
rule and its never-empty fallback.
"""

from __future__ import annotations

import pytest

from songforge.radio.selection import pick_static


def test_picks_the_only_non_recent_candidate() -> None:
    chosen = pick_static(["a", "b", "c"], recent_ids={"a", "b"})
    assert chosen == "c"


def test_never_picks_a_recent_candidate_when_a_non_recent_one_exists() -> None:
    chosen = pick_static(["a", "b", "c"], recent_ids={"a"})
    assert chosen not in {"a"}
    assert chosen in {"b", "c"}


def test_never_empty_replays_current_when_every_candidate_is_recent() -> None:
    chosen = pick_static(["a", "b"], recent_ids={"a", "b"}, current_id="a")
    assert chosen == "a"


def test_never_empty_falls_back_to_a_candidate_when_all_recent_and_no_current() -> None:
    chosen = pick_static(["a", "b"], recent_ids={"a", "b"}, current_id=None)
    # No prior song to replay (e.g. cold start) — must still return something playable.
    assert chosen in {"a", "b"}


def test_never_empty_replays_the_only_song_in_the_library() -> None:
    chosen = pick_static(["solo"], recent_ids=set(), current_id="solo")
    assert chosen == "solo"


def test_raises_on_an_empty_candidate_set() -> None:
    """There is nothing to ever play; the coordinator handles the no-songs-at-all
    idle case before calling this function."""
    with pytest.raises(ValueError):
        pick_static([], recent_ids=set())


def test_current_id_in_recent_does_not_block_a_non_recent_pick() -> None:
    """Recycling ``current_id`` is the LAST resort: a non-recent option always wins
    first, even when ``current_id`` itself happens to be in ``recent_ids``."""
    chosen = pick_static(["a", "b"], recent_ids={"a"}, current_id="a")
    assert chosen == "b"


def test_ignores_recent_ids_that_are_not_in_the_candidate_set() -> None:
    """A stale id in the recent set (e.g. a song since removed from the catalog) must
    not starve selection when unaffected candidates remain."""
    chosen = pick_static(["a", "b"], recent_ids={"not-a-candidate"})
    assert chosen in {"a", "b"}
