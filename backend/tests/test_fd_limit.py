"""Open-file limit raise at process start (songforge.fd_limit).

Docker commonly starts containers with a soft RLIMIT_NOFILE of 1024 and a much higher
hard limit. Node raises its own soft limit to the hard one at startup; Python does not,
so each web replica stalled at ~1000 held-open SSE connections (1024/1024 fds in use)
while the Node frontends sat at 524288. The web and worker entry points now raise the
soft limit themselves.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from songforge import fd_limit
from songforge.fd_limit import FdLimit, raise_open_file_limit

INFINITY = -1


class FakeResource:
    """Stand-in for the stdlib ``resource`` module."""

    RLIMIT_NOFILE = 7
    RLIM_INFINITY = INFINITY

    def __init__(self, soft: int, hard: int, refuse: bool = False) -> None:
        self.limits = (soft, hard)
        self.refuse = refuse
        self.set_calls: list[tuple[int, tuple[int, int]]] = []

    def getrlimit(self, which: int) -> tuple[int, int]:
        assert which == self.RLIMIT_NOFILE
        return self.limits

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        self.set_calls.append((which, limits))
        if self.refuse:
            raise ValueError("not allowed")
        self.limits = limits


def test_raises_soft_limit_to_hard_limit() -> None:
    res = FakeResource(soft=1024, hard=524288)
    result = raise_open_file_limit(res)
    assert res.set_calls == [(FakeResource.RLIMIT_NOFILE, (524288, 524288))]
    assert result == FdLimit(before=1024, after=524288, hard=524288)


def test_unlimited_hard_limit_is_capped() -> None:
    # Setting soft to RLIM_INFINITY fails on Linux (bounded by fs.nr_open).
    res = FakeResource(soft=1024, hard=INFINITY)
    result = raise_open_file_limit(res, ceiling=1_048_576)
    assert res.set_calls == [(FakeResource.RLIMIT_NOFILE, (1_048_576, INFINITY))]
    assert result.after == 1_048_576


def test_hard_limit_above_ceiling_is_capped() -> None:
    res = FakeResource(soft=1024, hard=10_000_000)
    assert raise_open_file_limit(res, ceiling=1_048_576).after == 1_048_576


@pytest.mark.parametrize("soft", [524288, 600000])
def test_never_lowers_or_rewrites_a_sufficient_limit(soft: int) -> None:
    res = FakeResource(soft=soft, hard=524288)
    result = raise_open_file_limit(res)
    assert res.set_calls == []
    assert result == FdLimit(before=soft, after=soft, hard=524288)


def test_refused_raise_keeps_the_old_limit_without_crashing() -> None:
    res = FakeResource(soft=1024, hard=524288, refuse=True)
    result = raise_open_file_limit(res)
    assert result == FdLimit(before=1024, after=1024, hard=524288)


def test_raised_flag() -> None:
    assert FdLimit(before=1024, after=4096, hard=4096).raised is True
    assert FdLimit(before=4096, after=4096, hard=4096).raised is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux rlimits")
def test_real_process_started_with_a_low_soft_limit_gets_raised() -> None:
    # A genuine child process under `ulimit -Sn 256`, using the real resource module.
    src = Path(fd_limit.__file__).resolve().parents[1]
    code = (
        "import resource; from songforge.fd_limit import raise_open_file_limit; "
        "r = raise_open_file_limit(); "
        "print(r.before, resource.getrlimit(resource.RLIMIT_NOFILE)[0], r.hard)"
    )
    out = subprocess.run(
        ["sh", "-c", f'ulimit -Sn 256 && exec "{sys.executable}" -c "{code}"'],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(src)},
        check=True,
    ).stdout.split()
    before, now, hard = (int(x) for x in out)
    assert before == 256
    assert now > 256
    assert now == min(hard, 1_048_576) or hard == -1


# ── entry points ─────────────────────────────────────────────────────────────────


def test_web_entry_point_raises_limit_before_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    import songforge.web.__main__ as web_main

    order: list[str] = []
    monkeypatch.setattr(
        web_main, "raise_open_file_limit", lambda: order.append("raise") or FdLimit(1024, 4096, 4096)
    )
    monkeypatch.setattr(web_main.uvicorn, "run", lambda *a, **k: order.append("serve"))
    web_main.main()
    assert order == ["raise", "serve"]


def test_worker_entry_point_raises_limit_before_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    import songforge.worker.main as worker_main

    order: list[str] = []
    monkeypatch.setattr(
        worker_main, "raise_open_file_limit", lambda: order.append("raise") or FdLimit(1024, 4096, 4096)
    )
    monkeypatch.setattr(worker_main, "start_worker_metrics_server", lambda s: order.append("metrics"))

    def fake_run(coro: object) -> None:
        order.append("loops")
        coro.close()  # type: ignore[attr-defined]

    monkeypatch.setattr(worker_main.asyncio, "run", fake_run)
    worker_main.main()
    assert order[:2] == ["raise", "metrics"] and "loops" in order
