"""Per-request fault injection switches for the MusicGPT simulator (issue #11).

Faults are selected with the ``X-Sim-Fault`` request header on the create call — the *one*
documented, deterministic mechanism (chosen over parsing magic markers out of free-text
prompts, which would risk colliding with real user content). There is no separate
"instant mode" switch: ``X-Sim-Delay-Seconds: 0`` covers it, and composes with any fault
that still delivers a webhook.
"""

from __future__ import annotations

import enum

#: Request header carrying the fault selector. Absent or unrecognised value => happy path.
FAULT_HEADER = "X-Sim-Fault"
#: Request header overriding the configured webhook delay for this request, in seconds.
#: ``0`` is instant mode: the webhook is scheduled with no delay.
DELAY_HEADER = "X-Sim-Delay-Seconds"


class Fault(str, enum.Enum):
    """Selectable fault-injection modes (issue #11 fault matrix).

    Each maps to a pipeline path exercised later, once the consumers land:
    - WEBHOOK_NEVER_ARRIVES -> watchdog /byId poll
    - URL_EXPIRES_BEFORE_INGEST -> /byId URL refresh
    - ERROR / FAILED -> refund + notify
    - DELAYED_WEBHOOK / DUPLICATE_WEBHOOK -> idempotency
    - RATE_LIMIT_429 -> retriable, no charge (nice-to-have)
    """

    NONE = "none"
    WEBHOOK_NEVER_ARRIVES = "webhook-never-arrives"
    URL_EXPIRES_BEFORE_INGEST = "url-expires-before-ingest"
    ERROR = "error"
    FAILED = "failed"
    DELAYED_WEBHOOK = "delayed-webhook"
    DUPLICATE_WEBHOOK = "duplicate-webhook"
    RATE_LIMIT_429 = "rate-limit-429"

    @classmethod
    def from_header(cls, value: str | None) -> Fault:
        """Parse the ``X-Sim-Fault`` header value; unrecognised/absent => :attr:`NONE`."""
        if value is None:
            return cls.NONE
        try:
            return cls(value)
        except ValueError:
            return cls.NONE
