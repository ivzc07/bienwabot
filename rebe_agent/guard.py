"""The call-rate guard: a loop detector wearing a counter's clothes.

Section 7 of `docs/wayfinder/token-budget-spec.md` decides there is no dollar
cap, because the thing worth defending against is a runaway loop - a webhook
replay storm, Rebe classifying her own messages, a retry loop against a failing
endpoint - and the damage a loop does is a banned WhatsApp number, not a bill.
Steady state is about 175 calls a day.

    > 600 calls   tell the maintainer, keep running. It may just be a busy group.
    > 2,000 calls stop calling DeepSeek until tomorrow. The process, the
                  heartbeat, and the scheduler all stay alive.

Both thresholds are read from the persisted counter rather than from process
memory, so a restart mid-storm does not hand the loop a fresh allowance.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date

from rebe_agent.alerts import Alerter
from rebe_agent.clock import Clock
from rebe_agent.usage import CallType, CallUsage, UsageStore

logger = logging.getLogger("rebe_agent.guard")

ALERT_THRESHOLD = 600
"""Calls in a local day that earn a maintainer alert. Calls continue."""

STOP_THRESHOLD = 2000
"""Calls in a local day after which DeepSeek is left alone until tomorrow."""


class DailyCallCeilingError(RuntimeError):
    """The day's call ceiling is spent. Raised instead of making the call."""

    def __init__(self, day: date, calls: int, ceiling: int) -> None:
        self.day = day
        self.calls = calls
        self.ceiling = ceiling
        super().__init__(
            f"DeepSeek is stopped for {day.isoformat()}: {calls} calls made, "
            f"ceiling is {ceiling}. Calls resume tomorrow."
        )


@dataclass(frozen=True, slots=True)
class Reservation:
    """A counted call, waiting for its usage.

    Carries the day the call was counted against so that a request straddling
    midnight books its tokens on the same day as its count.
    """

    day: date
    call_type: CallType
    calls_today: int


class CallRateGuard:
    """Counts every DeepSeek call, and stops the bot calling when a day runs away."""

    def __init__(
        self,
        store: UsageStore,
        clock: Clock,
        alerter: Alerter,
        *,
        alert_threshold: int = ALERT_THRESHOLD,
        stop_threshold: int = STOP_THRESHOLD,
    ) -> None:
        if not 0 < alert_threshold <= stop_threshold:
            raise ValueError(
                f"thresholds must satisfy 0 < alert <= stop, "
                f"got alert={alert_threshold} stop={stop_threshold}"
            )
        self._store = store
        self._clock = clock
        self._alerter = alerter
        self._alert_threshold = alert_threshold
        self._stop_threshold = stop_threshold
        # Reading the day's count and adding to it is two statements, and both
        # legs share this guard inside one process (the single-replica invariant
        # in the deployment spec). Without the lock, a webhook and a news tick
        # landing together could both read 1,999 and both call.
        self._turnstile = asyncio.Lock()

    async def reserve(self, call_type: CallType) -> Reservation:
        """Count one call, or refuse the day.

        Raises `DailyCallCeilingError` when the day is already spent, which the
        brain turns into a failed call and the caller turns into silence.
        """
        async with self._turnstile:
            day = self._clock.now().date()

            before = await self._store.calls_on(day)
            if before >= self._stop_threshold:
                logger.warning(
                    "DeepSeek call refused: %s calls already made on %s (ceiling %s)",
                    before,
                    day.isoformat(),
                    self._stop_threshold,
                )
                raise DailyCallCeilingError(day, before, self._stop_threshold)

            after = await self._store.record_call(day, call_type)

        await self._announce_crossings(day, before, after)
        return Reservation(day=day, call_type=call_type, calls_today=after)

    async def record_usage(self, reservation: Reservation, usage: CallUsage) -> None:
        """Book what the call actually cost against the day it was counted on."""
        await self._store.record_usage(reservation.day, reservation.call_type, usage)

    async def _announce_crossings(self, day: date, before: int, after: int) -> None:
        """Alert once per threshold per day.

        Firing on the crossing rather than on the level is what keeps a restart
        from re-alerting: after a restart the count is already above the line, so
        no crossing happens and the maintainer is not told twice.
        """
        if before < self._alert_threshold <= after:
            await self._alerter.alert(
                f"DeepSeek calls passed {self._alert_threshold} on {day.isoformat()} "
                f"({after} so far). Expected is about 175/day. Still calling; "
                f"the hard stop is {self._stop_threshold}."
            )
        if before < self._stop_threshold <= after:
            await self._alerter.alert(
                f"DeepSeek calls reached {self._stop_threshold} on {day.isoformat()}. "
                f"Rebe has stopped calling DeepSeek until tomorrow and will stay silent. "
                f"The process, the heartbeat, and the scheduler keep running."
            )
