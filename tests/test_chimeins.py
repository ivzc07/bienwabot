"""The chime-in budget: how often she may join a conversation nobody invited her to.

Four separate rules live in one object here, and the tests keep them separate:
the probability that makes her selective, the daily ceiling that is absolute
whatever the probability says, the cooldown that stops two landing together, and
the band of the night she does not volunteer anything in at all.

Nothing here is flaky. The clock is moved by hand and the draw comes from an
injected `random.Random`, so "about a quarter" is an assertion rather than a hope.
"""

from __future__ import annotations

import random
from datetime import timedelta

from rebe_agent.chimeins import Allowance, ChimeInBudget, InMemoryChimeInLog
from rebe_agent.clock import ManualClock
from tests.support import GROUP, MEXICO_CITY, NOON

SEED = 20260725

SHIPPED = Allowance()
"""The numbers Rebe actually runs with, for the tests that are about them."""

ALWAYS = Allowance(cooldown=timedelta(0), per_day=10_000)
"""Everything but the roll lifted, for the tests that are about the roll."""


def budget(
    clock: ManualClock,
    log: InMemoryChimeInLog | None = None,
    *,
    rng: random.Random | None = None,
    allowance: Allowance | None = None,
) -> ChimeInBudget:
    return ChimeInBudget(
        log or InMemoryChimeInLog(),
        clock,
        rng=rng or random.Random(SEED),
        allowance=allowance,
    )


class Rolls(random.Random):
    """An RNG whose draws a test names outright, so no seed has to be reverse-engineered.

    Exhausted draws come back as 1.0, which is above every probability in
    `[0, 1)` - so a test that names one "yes" gets exactly one.
    """

    def __init__(self, *draws: float) -> None:
        super().__init__()
        self._draws = list(draws)

    def random(self) -> float:
        return self._draws.pop(0) if self._draws else 1.0


async def test_an_eligible_message_can_become_a_chime_in() -> None:
    clock = ManualClock(NOON)

    assert await budget(clock, rng=Rolls(0.1)).refuses() is None


async def test_most_eligible_messages_get_nothing() -> None:
    """The rate the reply policy asks for: a quarter, so she reads as selective
    rather than as somebody watching the group all day."""
    clock = ManualClock(NOON)
    allowed = budget(clock, rng=random.Random(SEED), allowance=ALWAYS)

    said_yes = 0
    for _ in range(2_000):
        said_yes += await allowed.refuses() is None

    assert 0.22 <= said_yes / 2_000 <= 0.28, f"{said_yes} of 2,000 is not about a quarter"


async def test_the_daily_ceiling_holds_whatever_the_roll_says() -> None:
    """The ceiling is absolute: every roll below says yes, and it is still no."""
    clock = ManualClock(NOON)
    log = InMemoryChimeInLog()
    allowance = Allowance(cooldown=timedelta(0))
    day = budget(clock, log, rng=Rolls(*[0.0] * (allowance.per_day + 1)), allowance=allowance)

    for _ in range(allowance.per_day):
        assert await day.refuses() is None
        await day.spend(GROUP)

    refusal = await day.refuses()
    assert refusal is not None and "today already" in refusal


async def test_two_chime_ins_never_land_back_to_back() -> None:
    clock = ManualClock(NOON)
    log = InMemoryChimeInLog()
    day = budget(clock, log, rng=Rolls(0.0, 0.0))

    await day.spend(GROUP)
    clock.advance(SHIPPED.cooldown - timedelta(minutes=1))

    refusal = await day.refuses()
    assert refusal is not None and "too close" in refusal


async def test_the_cooldown_does_expire() -> None:
    clock = ManualClock(NOON)
    log = InMemoryChimeInLog()
    day = budget(clock, log, rng=Rolls(0.0, 0.0))

    await day.spend(GROUP)
    clock.advance(SHIPPED.cooldown)

    assert await day.refuses() is None


async def test_the_roll_is_what_keeps_her_quiet_when_nothing_else_does() -> None:
    clock = ManualClock(NOON)

    refusal = await budget(clock, rng=Rolls(0.9)).refuses()

    assert refusal is not None and "roll" in refusal


async def test_she_volunteers_nothing_in_the_near_silent_hours() -> None:
    """ "02:00-06:00: near-silent ... replies only if directly addressed." The
    pacer widens the gap between everything in that band; only this tier can say
    a message should not be sent there at all."""
    clock = ManualClock(NOON.replace(hour=3, minute=30), MEXICO_CITY)

    refusal = await budget(clock, rng=Rolls(0.0)).refuses()

    assert refusal is not None and "near-silent" in refusal


async def test_the_morning_opens_again() -> None:
    clock = ManualClock(NOON.replace(hour=6, minute=1), MEXICO_CITY)

    assert await budget(clock, rng=Rolls(0.0)).refuses() is None


async def test_the_day_turns_over_at_local_midnight_not_at_utc_midnight() -> None:
    """Noon in Mexico City is already 18:00 UTC, so a count kept in UTC would
    hand her a fresh allowance at 18:00 local - in the middle of the evening."""
    clock = ManualClock(NOON, MEXICO_CITY)
    log = InMemoryChimeInLog()
    allowance = Allowance(cooldown=timedelta(0))
    day = budget(clock, log, rng=Rolls(*[0.0] * 10), allowance=allowance)

    for _ in range(allowance.per_day):
        await day.spend(GROUP)
    clock.advance(timedelta(hours=7))

    assert await day.refuses() is not None, "19:00 local is the same day as noon"


async def test_a_fresh_local_day_is_a_fresh_allowance() -> None:
    clock = ManualClock(NOON, MEXICO_CITY)
    log = InMemoryChimeInLog()
    allowance = Allowance(cooldown=timedelta(0))
    day = budget(clock, log, rng=Rolls(*[0.0] * 10), allowance=allowance)

    for _ in range(allowance.per_day):
        await day.spend(GROUP)
    clock.advance(timedelta(hours=19))

    assert await day.refuses() is None, "07:00 the next morning is a new day"


async def test_the_count_outlives_the_budget_that_wrote_it() -> None:
    """The whole reason this is a log rather than a counter on the object: a
    restart must not hand her three more chime-ins."""
    clock = ManualClock(NOON)
    log = InMemoryChimeInLog()
    allowance = Allowance(cooldown=timedelta(0))
    before = budget(clock, log, rng=Rolls(*[0.0] * 10), allowance=allowance)
    for _ in range(allowance.per_day):
        await before.spend(GROUP)

    after_restart = budget(clock, log, rng=Rolls(*[0.0] * 10), allowance=allowance)

    assert await after_restart.refuses() is not None


def test_the_shipped_ceiling_sits_inside_the_band_the_playbook_names() -> None:
    """Section 2 of the anti-ban playbook caps unprompted chime-ins at 2-3 a day,
    and the reply policy calls that ceiling absolute."""
    assert 2 <= SHIPPED.per_day <= 3
