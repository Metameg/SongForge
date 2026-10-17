"""Fault-injectable fake of the MusicGPT generation API (issue #11).

A separate, standalone service the whole pipeline develops and tests against for free and
deterministically — selected purely by base-URL config (``MUSICGPT_BASE_URL``). See
``.orchestrator/CONTEXT.md`` (issue #11) for the full contract and fault matrix this package
implements.
"""

from __future__ import annotations
