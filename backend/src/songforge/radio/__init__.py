"""Static radio: the ``radio_state`` pointer, its selection logic, and the coordinator.

See ``songforge.radio.state`` (pointer + read model), ``songforge.radio.selection``
(pure next-song choice), and ``songforge.radio.coordinator`` (single-leader advance loop
wired into ``worker/main.py``) — issue #8.
"""

from __future__ import annotations
