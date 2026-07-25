"""Time, as a dependency.

The pacer, the quiet-hours window and the news scheduler all reason about
"now" in `America/Mexico_City`. They read it through a `Clock` rather than
calling `datetime.now()` inline, so tests can place the process at 03:00 on a
Tuesday without waiting for Tuesday.

`zone` is always a `tzinfo` object; the string name it came from lives on
`Settings.timezone`.

*Waiting* is the same problem in the other direction. The pacer spends most of
its life asleep - a typing pause, a beat before a first message, a minute window
draining - and a test that asserted those by actually waiting would be a slow
test that proves nothing about the numbers. So sleeping goes through a `Sleeper`
too, and a test hands it one that moves a `ManualClock` instead of the world.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol


class Clock(Protocol):
    """A source of the current instant, in a known zone."""

    @property
    def zone(self) -> tzinfo:
        """The zone `now()` reports in."""

    def now(self) -> datetime:
        """The current instant, timezone-aware."""


@dataclass(frozen=True)
class SystemClock:
    """The real clock, reading the wall clock in a fixed zone."""

    zone: tzinfo

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
    def zone(self) -> tzinfo:
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


class Sleeper(Protocol):
    """A way to wait, so that waiting can be replaced in a test."""

    async def sleep(self, seconds: float) -> None:
        """Wait roughly `seconds`. A non-positive value waits not at all."""


class RealSleeper:
    """The real wait. Yields to the event loop, so the other leg keeps running."""

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class ManualSleeper:
    """Moves a `ManualClock` forward instead of waiting, and remembers by how much."""

    def __init__(self, clock: ManualClock) -> None:
        self._clock = clock
        self.slept: list[float] = []

    @property
    def total(self) -> float:
        """Every second this would have spent waiting."""
        return sum(self.slept)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.slept.append(seconds)
        self._clock.advance(timedelta(seconds=seconds))
