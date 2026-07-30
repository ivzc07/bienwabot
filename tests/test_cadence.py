"""The dawn roll: where the day's post times land, and where they never land.

Every assertion here is about a *distribution*, so the draws are seeded rather
than mocked: the same seed gives the same day back, and a few hundred simulated
days say more about "clustered toward the middle, never outside the edges" than
one hand-picked draw could.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from itertools import pairwise

import pytest

from rebe_agent.cadence import (
    WAKING_CLOSES,
    WAKING_OPENS,
    WEEKDAY_WINDOWS,
    WEEKEND_WINDOWS,
    Cadence,
    PostWindow,
    dense_cadence,
    draw_plan,
    jittered_gap,
    moment_on,
    slot_windows,
)
from tests.support import MEXICO_CITY

SEED = 20260729

WEDNESDAY = date(2026, 7, 29)
SATURDAY = date(2026, 7, 25)
SUNDAY = date(2026, 7, 26)

DAYS = 300
"""How many simulated days a distribution claim is checked over."""


def roll(day: date, rng: random.Random, cadence: Cadence | None = None) -> tuple[datetime, ...]:
    """One day's drawn times, in order."""
    plan = draw_plan(day, zone=MEXICO_CITY, rng=rng, cadence=cadence)
    return tuple(slot.at for slot in plan.slots)


def only(window: PostWindow) -> Cadence:
    """A cadence of one window, so a claim about that window stands alone."""
    return Cadence(weekday=(window,), weekend=(window,))


# --- the shape of the day ----------------------------------------------------


def test_the_shipped_windows_are_the_ones_the_spec_names() -> None:
    """Section 2 of the cadence spec, as a table. A typo here is a wrong day."""
    assert [(w.name, str(w)) for w in WEEKDAY_WINDOWS] == [
        ("morning", "08:00-10:30"),
        ("midday", "13:00-15:00"),
        ("evening", "18:00-20:00"),
        ("late", "21:30-23:00"),
    ]
    assert [(w.name, str(w)) for w in WEEKEND_WINDOWS] == [
        ("midday", "14:00-16:30"),
        ("evening", "19:30-22:00"),
    ]


def test_a_weekday_draws_one_time_in_each_of_the_four_windows() -> None:
    plan = draw_plan(WEDNESDAY, zone=MEXICO_CITY, rng=random.Random(SEED))

    assert [slot.window for slot in plan.slots] == ["morning", "midday", "evening", "late"]
    assert plan.day == WEDNESDAY


def test_a_thirty_minute_dense_day_fills_wake_to_quiet() -> None:
    """The volume experiment: one short firing window every half hour."""
    windows = slot_windows(30)
    assert windows[0].opens == WAKING_OPENS
    assert windows[0].closes == time(8, 5)
    assert windows[1].opens == time(8, 30)
    assert len(windows) == 30  # 08:00, 08:30, ... 22:30

    cadence = dense_cadence(30)
    plan = draw_plan(WEDNESDAY, zone=MEXICO_CITY, rng=random.Random(SEED), cadence=cadence)

    assert len(plan.slots) == 30
    assert cadence.daily_stop == 30
    assert all(
        later.at - earlier.at >= cadence.gap[0] for earlier, later in pairwise(plan.slots)
    )


@pytest.mark.parametrize("day", [SATURDAY, SUNDAY])
def test_a_weekend_drops_the_morning_window_and_shifts_the_rest_later(day: date) -> None:
    """Real people wake later on a Saturday, and AI news dries up on one."""
    plan = draw_plan(day, zone=MEXICO_CITY, rng=random.Random(SEED))

    assert [slot.window for slot in plan.slots] == ["midday", "evening"]
    assert all(slot.at.time() >= time(14, 0) for slot in plan.slots)


@pytest.mark.parametrize("window", WEEKDAY_WINDOWS + WEEKEND_WINDOWS, ids=lambda w: str(w))
def test_a_drawn_time_never_falls_outside_its_window(window: PostWindow) -> None:
    """The window is half-open, like the pacer's: the closing minute itself is
    already the next thing. It matters at 23:00, where the overnight hold starts
    and a post drawn on that second would be written and then refused."""
    rng = random.Random(SEED)
    opens = moment_on(WEDNESDAY, window.opens, MEXICO_CITY)
    closes = moment_on(WEDNESDAY, window.closes, MEXICO_CITY)

    drawn = [roll(WEDNESDAY, rng, only(window))[0] for _ in range(DAYS)]

    assert all(opens <= at < closes for at in drawn)


@pytest.mark.parametrize("window", WEEKDAY_WINDOWS + WEEKEND_WINDOWS, ids=lambda w: str(w))
def test_drawn_times_cluster_toward_the_middle_of_the_window(window: PostWindow) -> None:
    """The central tendency is the point: habits, not a flat chance across the
    window. A Gaussian with sigma a fifth of the width puts about two thirds of
    the draws within one sigma of the midpoint, and the clipped tails only pull
    that number up."""
    rng = random.Random(SEED)
    middle = moment_on(WEDNESDAY, window.opens, MEXICO_CITY) + window.span / 2
    sigma = window.span / 5

    drawn = [roll(WEDNESDAY, rng, only(window))[0] for _ in range(DAYS)]

    near = sum(abs(at - middle) <= sigma for at in drawn)
    average = middle + sum((at - middle for at in drawn), timedelta()) / len(drawn)
    assert near / len(drawn) > 0.6
    assert abs(average - middle) < window.span / 20


def test_the_exact_minute_does_not_repeat() -> None:
    """A time that came back twice in three hundred days would be a fingerprint."""
    rng = random.Random(SEED)

    mornings = [roll(WEDNESDAY, rng)[0] for _ in range(DAYS)]

    assert len(set(mornings)) > DAYS * 0.9


def test_two_rolls_of_the_same_day_produce_different_times() -> None:
    """Rolling twice is not something the agent does, but a plan that came out
    the same both times would mean the day was a constant with extra steps."""
    rng = random.Random(SEED)

    assert roll(WEDNESDAY, rng) != roll(WEDNESDAY, rng)


def test_the_same_seed_gives_the_same_day_back() -> None:
    """What makes every other assertion here a claim about the code."""
    assert roll(WEDNESDAY, random.Random(SEED)) == roll(WEDNESDAY, random.Random(SEED))


# --- the overnight hold ------------------------------------------------------


@pytest.mark.parametrize("day", [WEDNESDAY, SATURDAY, SUNDAY])
def test_nothing_is_ever_drawn_between_eleven_at_night_and_eight_in_the_morning(
    day: date,
) -> None:
    """Stricter than the anti-ban near-silent band, and deliberately so: sleep is
    the strongest human signal there is."""
    rng = random.Random(SEED)

    for _ in range(DAYS):
        for at in roll(day, rng):
            assert WAKING_OPENS <= at.time() < WAKING_CLOSES


def test_the_late_window_never_draws_the_stroke_of_eleven() -> None:
    """The one boundary that costs something: the pacer holds every post from
    23:00:00, so a slot drawn on that second would be summarised by DeepSeek and
    then refused. Forced by drawing the tail two thousand times over."""
    late = WEEKDAY_WINDOWS[-1]
    rng = random.Random(SEED)
    edge = moment_on(WEDNESDAY, WAKING_CLOSES, MEXICO_CITY)

    drawn = [roll(WEDNESDAY, rng, only(late))[0] for _ in range(2000)]

    assert max(drawn) < edge
    assert max(drawn) >= edge - timedelta(minutes=1), "the edge is still reachable"


def test_a_window_that_reaches_into_the_night_is_refused() -> None:
    """The overnight hold is a property of the code, not of the constants: a
    posture that widened a window past 23:00 would not start."""
    with pytest.raises(ValueError, match="23:00"):
        Cadence(weekday=(PostWindow("insomnia", time(22, 0), time(23, 30)),))

    with pytest.raises(ValueError, match="08:00"):
        Cadence(weekday=(PostWindow("madrugada", time(3, 0), time(5, 0)),))


def test_a_window_that_closes_before_it_opens_is_refused() -> None:
    with pytest.raises(ValueError, match="closes"):
        Cadence(weekday=(PostWindow("backwards", time(15, 0), time(13, 0)),))


def test_two_windows_cannot_share_a_name() -> None:
    """The name is how a slot is found again after a restart, so it is a key."""
    twice = PostWindow("midday", time(13, 0), time(15, 0))
    with pytest.raises(ValueError, match="midday"):
        Cadence(weekday=(twice, twice))


# --- the global minimum gap --------------------------------------------------


@pytest.mark.parametrize("day", [WEDNESDAY, SATURDAY])
def test_no_two_planned_times_are_closer_than_the_minimum_gap(day: date) -> None:
    """The whole reason the day is rolled at once rather than window by window:
    independent draws cannot see each other, and two posts ten minutes apart
    across a window boundary is exactly the burst the envelope forbids."""
    rng = random.Random(SEED)
    lowest, _ = Cadence().gap

    for _ in range(DAYS):
        drawn = roll(day, rng)
        gaps = [later - earlier for earlier, later in pairwise(drawn)]
        assert all(gap >= lowest for gap in gaps), f"{drawn}"


def test_the_minimum_gap_itself_varies() -> None:
    """A fixed 75 minutes would be a periodic rhythm with extra steps."""
    rng = random.Random(SEED)
    low, high = Cadence().gap

    gaps = [jittered_gap(rng, Cadence()) for _ in range(DAYS)]

    assert all(low <= gap <= high for gap in gaps)
    assert len(set(gaps)) > DAYS * 0.9
    assert min(gaps) < low + timedelta(minutes=2)
    assert max(gaps) > high - timedelta(minutes=2)


def test_a_slot_that_cannot_be_spaced_is_dropped_rather_than_bunched() -> None:
    """Two windows an hour apart end to end cannot both hold a post, so the later
    one is given up after a few redraws. The target is soft; the gap is not."""
    cramped = Cadence(
        weekday=(
            PostWindow("first", time(8, 0), time(8, 30)),
            PostWindow("second", time(8, 30), time(9, 0)),
        )
    )

    plan = draw_plan(WEDNESDAY, zone=MEXICO_CITY, rng=random.Random(SEED), cadence=cramped)

    assert [slot.window for slot in plan.slots] == ["first"]


def test_a_dropped_slot_does_not_drag_the_rest_of_the_day_with_it() -> None:
    """The dropped slot is measured out of the plan, not left in it as a ghost the
    next window has to clear."""
    cramped = Cadence(
        weekday=(
            PostWindow("first", time(8, 0), time(8, 30)),
            PostWindow("second", time(8, 30), time(9, 0)),
            PostWindow("third", time(13, 0), time(15, 0)),
        )
    )

    plan = draw_plan(WEDNESDAY, zone=MEXICO_CITY, rng=random.Random(SEED), cadence=cramped)

    assert [slot.window for slot in plan.slots] == ["first", "third"]


# --- what a slot carries -----------------------------------------------------


def test_a_slot_knows_the_edge_it_must_not_drift_far_past() -> None:
    """A deferred post may run a little past its window and no further, so the
    edge travels with the slot rather than being recomputed later."""
    plan = draw_plan(WEDNESDAY, zone=MEXICO_CITY, rng=random.Random(SEED))

    morning = plan.slots[0]
    assert morning.closes == moment_on(WEDNESDAY, time(10, 30), MEXICO_CITY)
    assert morning.at < morning.closes


def test_a_slot_may_run_half_an_hour_past_its_window_but_never_past_bedtime() -> None:
    """Section 5's grace and section 2's overnight hold disagree about the late
    window, and the hold wins: 23:30 is not a time Rebe posts at."""
    cadence = Cadence()
    plan = draw_plan(WEDNESDAY, zone=MEXICO_CITY, rng=random.Random(SEED), cadence=cadence)
    by_window = {slot.window: slot for slot in plan.slots}

    evening = cadence.deadline_for(by_window["evening"], MEXICO_CITY)
    late = cadence.deadline_for(by_window["late"], MEXICO_CITY)

    assert evening == moment_on(WEDNESDAY, time(20, 30), MEXICO_CITY)
    assert late == moment_on(WEDNESDAY, WAKING_CLOSES, MEXICO_CITY)


def test_a_freshly_drawn_slot_is_still_waiting_to_happen() -> None:
    plan = draw_plan(WEDNESDAY, zone=MEXICO_CITY, rng=random.Random(SEED))

    assert plan.pending == plan.slots
