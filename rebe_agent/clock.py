"""Time, as a dependency.

The pacer, the quiet-hours window and the news scheduler all reason about
"now" in `America/Mexico_City`. They read it through a `Clock` rather than
calling `datetime.now()` inline, so tests can place the process at 03:00 on a
Tuesday without waiting for Tuesday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol, runtime_checkable

# Near-silent window from the anti-ban envelope (deployment spec, section 2.2).
QUIET_HOURS_START = 2
QUIET_HOURS_END = 6


@runtime_checkable
class Clock(Protocol):
    """A source of the current instant, in a known zone."""

    @property
    def timezone(self) -> tzinfo:
        """The zone `now()` reports in."""

    def now(self) -> datetime:
        """The current instant, timezone-aware."""


@dataclass(frozen=True)
class SystemClock:
    """The real clock, reading the wall clock in a fixed zone."""

    zone: tzinfo

    @property
    def timezone(self) -> tzinfo:
        return self.zone

    def now(self) -> datetime:
        return datetime.now(self.zone)


class ManualClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, instant: datetime, tz: tzinfo | None = None) -> None:
        self._zone: tzinfo = tz or instant.tzinfo or UTC
        self._instant = self._normalise(instant)

    def _normalise(self, instant: datetime) -> datetime:
        if instant.tzinfo is None:
            return instant.replace(tzinfo=self._zone)
        return instant.astimezone(self._zone)

    @property
    def timezone(self) -> tzinfo:
        return self._zone

    def now(self) -> datetime:
        return self._instant

    def advance(self, delta: timedelta) -> None:
        """Move forward by `delta`. Time does not run backwards."""
        if delta < timedelta(0):
            raise ValueError(f"a clock cannot run backwards: {delta!r}")
        self._instant += delta

    def set(self, instant: datetime) -> None:
        """Place the clock at an exact instant."""
        self._instant = self._normalise(instant)


def is_quiet_hours(clock: Clock) -> bool:
    """True inside the near-silent 02:00-06:00 local window."""
    hour = clock.now().astimezone(clock.timezone).hour
    return QUIET_HOURS_START <= hour < QUIET_HOURS_END
