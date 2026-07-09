"""Wiper contract.

Every wiper implements the same three-step discipline so the safety guarantees
hold uniformly:

    plan()   -> WipePlan   # describe what WOULD change. MUST NOT mutate.
    commit() -> WipePlan   # do it. MUST only run after a snapshot (see core).
    verify() -> bool       # confirm it took effect.

The core orchestrator is what enforces "snapshot before commit" and the
keep-list — individual wipers just report and act on their own state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class WipePlan:
    """The result of ``plan()`` (dry-run) or ``commit()`` (what was done)."""

    scope: str
    description: str
    item_count: int = 0
    reversible: bool = True
    notes: tuple[str, ...] = ()


class Wiper(ABC):
    """Base class for a single unit of resettable state."""

    #: the ``--scope`` this wiper answers to.
    scope: str

    @abstractmethod
    def plan(self) -> WipePlan:
        """Return what *would* change. Never mutates state."""

    @abstractmethod
    def commit(self) -> WipePlan:
        """Perform the wipe. Only called by the core *after* a snapshot."""

    @abstractmethod
    def verify(self) -> bool:
        """Return True if the wipe is confirmed to have taken effect."""
