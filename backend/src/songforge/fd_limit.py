"""Raise this process's open-file soft limit to its hard limit at startup.

Every held-open SSE listener costs the web process one file descriptor. Docker commonly
starts containers with a soft ``RLIMIT_NOFILE`` of 1024 and a far higher hard limit;
Node raises its own soft limit to the hard one at startup, CPython does not. Left alone,
each web replica stalls at ~1000 listeners with all 1024 descriptors in use (measured on
the home dev server, where the Node frontends meanwhile sat at 524288).

Raising the soft limit up to the hard limit needs no privileges, so the web and worker
entry points do it themselves -- the same everywhere (laptop, home server, Railway),
with no compose or platform setting to keep in sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Linux refuses a soft limit of RLIM_INFINITY (it is bounded by fs.nr_open, 1048576 by
# default), so an unlimited or huge hard limit is capped here.
DEFAULT_CEILING = 1_048_576


class ResourceModule(Protocol):
    """The parts of the stdlib ``resource`` module this uses (injectable for tests)."""

    RLIMIT_NOFILE: int
    RLIM_INFINITY: int

    def getrlimit(self, resource: int, /) -> tuple[int, int]: ...

    def setrlimit(self, resource: int, limits: tuple[int, int], /) -> None: ...


@dataclass(frozen=True)
class FdLimit:
    """Soft limit before and after the raise, and the hard limit it was raised toward."""

    before: int
    after: int
    hard: int

    @property
    def raised(self) -> bool:
        return self.after > self.before


def raise_open_file_limit(
    res: ResourceModule | None = None, ceiling: int = DEFAULT_CEILING
) -> FdLimit:
    """Raise the soft open-file limit to ``min(hard, ceiling)``; never lower it.

    A refused raise is not fatal: the process keeps its old limit and the caller can
    log the returned :class:`FdLimit`.
    """
    if res is None:
        import resource  # noqa: PLC0415 - POSIX-only; imported where it is used

        mod: ResourceModule = resource
    else:
        mod = res

    soft, hard = mod.getrlimit(mod.RLIMIT_NOFILE)
    target = ceiling if hard == mod.RLIM_INFINITY else min(hard, ceiling)
    if soft != mod.RLIM_INFINITY and soft < target:
        try:
            mod.setrlimit(mod.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError):
            return FdLimit(before=soft, after=soft, hard=hard)
        return FdLimit(before=soft, after=target, hard=hard)
    return FdLimit(before=soft, after=soft, hard=hard)
