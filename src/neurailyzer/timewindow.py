"""Time windows: wipe only the state a harness accumulated in a span of time.

The neuralizer flash erases everything *since* a moment -- ``wipe session
--after 2026-11-01`` forgets this month and leaves October alone. ``--before``
is the retention cut (drop what is older than a date). Both together form a
window. A file is inside the window by its own modification time, read with
``lstat`` so a symlink is judged by the link, never by what it points at.

Boundaries are per FILE. A database file (``state_5.sqlite``) carries one
mtime for every row in it, so a window takes it whole or leaves it whole; the
wiper says so in its plan whenever a window touches such a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = ["TimeWindow", "TimeWindowError", "parse_when"]

_RELATIVE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.IGNORECASE)
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


class TimeWindowError(ValueError):
    """The moment could not be read. The message says what was expected."""


def parse_when(text: str, *, now: datetime | None = None) -> datetime:
    """Read a moment: ISO-8601 (``2026-11-01``, ``2026-11-01T04:00``, with or
    without a zone) or a span back from now (``30m``, ``12h``, ``7d``, ``2w``).

    A naive ISO time is the local clock, which is what a person typing a date
    means. The result is always zone-aware so it compares with file mtimes.
    """
    if not isinstance(text, str) or not text.strip():
        raise TimeWindowError("a moment is required: ISO-8601 time, or 30m / 12h / 7d / 2w")
    reference = now if now is not None else datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.astimezone()
    m = _RELATIVE.match(text)
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        if amount == 0:
            raise TimeWindowError(f"{text!r}: a span of zero is not a moment")
        return reference - timedelta(**{_UNITS[unit]: amount})
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise TimeWindowError(
            f"{text!r}: not an ISO-8601 time and not a span (30m / 12h / 7d / 2w)"
        ) from exc
    if when.tzinfo is None:
        when = when.astimezone()  # local clock, as typed
    return when


@dataclass(frozen=True)
class TimeWindow:
    """State modified at/after ``after`` and before ``before`` is in scope."""

    after: datetime | None = None
    before: datetime | None = None

    def __post_init__(self) -> None:
        for name, value in (("after", self.after), ("before", self.before)):
            if value is not None and value.tzinfo is None:
                raise TimeWindowError(f"{name} must be zone-aware")
        if self.after is not None and self.before is not None and self.after >= self.before:
            raise TimeWindowError(
                f"empty window: after {self.after.isoformat()} is not earlier than "
                f"before {self.before.isoformat()}"
            )

    @property
    def is_open(self) -> bool:
        """True when no bound is set -- everything is in scope."""
        return self.after is None and self.before is None

    def contains(self, mtime: float) -> bool:
        """True if a file modified at POSIX time *mtime* falls in the window."""
        if self.after is not None and mtime < self.after.timestamp():
            return False
        return not (self.before is not None and mtime >= self.before.timestamp())

    def describe(self) -> str:
        parts = []
        if self.after is not None:
            parts.append(f"modified at/after {self.after.isoformat(timespec='minutes')}")
        if self.before is not None:
            parts.append(f"modified before {self.before.isoformat(timespec='minutes')}")
        return " and ".join(parts) or "any time"
