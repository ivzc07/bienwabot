"""The day's post times, drawn at dawn.

Sections 1 to 3 of `docs/wayfinder/posting-cadence-spec.md` in one function:
pick the weekday or weekend window set, draw one time inside each window from a
Gaussian centred on its midpoint and clipped to its edges, and space the results
at least 75 to 90 jittered minutes apart.

**The whole day is drawn at once, not one window at a time.** Global spacing is
only enforceable with a global view. Independent per-window draws cannot see each
other and will eventually put two posts ten minutes apart across a window
boundary, which is exactly the burst the anti-ban envelope exists to forbid.

**The central tendency is the point.** A flat draw across a window is equally
likely to post at 08:00 or at 10:29; a Gaussian centred on 09:15 gives Rebe a
habit, which is what a person has. `sigma` is a fifth of the window, so the edges
sit two and a half sigma out and are reached a few times a year rather than never.

**Nothing is drawn between 23:00 and 08:00.** That is enforced here against
`WAKING_OPENS` and `WAKING_CLOSES` rather than left to the shipped constants: a
`Cadence` whose windows reach into the night will not build, so the overnight
hold is a property of the code. The pacer refuses such a send anyway, and the two
are deliberately not the same check - this one keeps the hour out of the plan, and
that one keeps it off the wire.

Randomness arrives as a `random.Random` and time arrives as a `date` plus a zone,
so a test can roll three hundred days of a Wednesday and read the distribution.

The posture numbers sections 4 and 6 add - how often the loop looks up from the
plan for breaking news, and the practical eight-post stop that bounds a day the
overrides made long - live on `Cadence` beside the rest of the day's shape, so
there is one object a ramp tightens rather than four constants in four modules.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import StrEnum

from rebe_agent.tiers import Tier

logger = logging.getLogger("rebe_agent.cadence")

DAWN = time(6, 0)
"""When the roll runs. The only fixed-time job in the system, per section 3.

It is fixed rather than jittered because it sends nothing: it draws times and
writes them down, and an observer of the group cannot see it happen at all.
"""

WAKING_OPENS = time(8, 0)
WAKING_CLOSES = time(23, 0)
"""The hours a post may be planned in. Stricter than the anti-ban near-silent
band of 02:00-06:00, because sleep is the strongest human signal available."""

SIGMA_DIVISOR = 5.0
"""`sigma = window_width / 5`, from section 3 step 2."""

RESOLUTION = timedelta(seconds=1)
"""The grain a time is drawn to, and the width of a window's closing edge."""


@dataclass(frozen=True, slots=True)
class PostWindow:
    """One loose stretch of the day, with a name a log line can carry.

    The name is also the key a slot is found again by after a restart, so it is
    unique within a day's window set.
    """

    name: str
    opens: time
    closes: time

    def __post_init__(self) -> None:
        if self.opens >= self.closes:
            raise ValueError(f"the {self.name} window closes ({self.closes}) before it opens")
        if self.opens < WAKING_OPENS:
            raise ValueError(
                f"the {self.name} window opens at {self.opens:%H:%M}, "
                f"before Rebe is awake at {WAKING_OPENS:%H:%M}"
            )
        if self.closes > WAKING_CLOSES:
            raise ValueError(
                f"the {self.name} window closes at {self.closes:%H:%M}, "
                f"after Rebe goes quiet at {WAKING_CLOSES:%H:%M}"
            )

    @property
    def span(self) -> timedelta:
        """How wide the window is."""
        return _since_midnight(self.closes) - _since_midnight(self.opens)

    def __str__(self) -> str:
        return f"{self.opens:%H:%M}-{self.closes:%H:%M}"


WEEKDAY_WINDOWS: tuple[PostWindow, ...] = (
    PostWindow("morning", time(8, 0), time(10, 30)),
    PostWindow("midday", time(13, 0), time(15, 0)),
    PostWindow("evening", time(18, 0), time(20, 0)),
    PostWindow("late", time(21, 30), time(23, 0)),
)
"""Coffee, lunch, after work when the group is most alive, winding down."""

WEEKEND_WINDOWS: tuple[PostWindow, ...] = (
    PostWindow("midday", time(14, 0), time(16, 30)),
    PostWindow("evening", time(19, 30), time(22, 0)),
)
"""The morning is dropped and the rest shift about two hours later. Two
independent reasons point the same way: people wake later on a Saturday, and AI
news genuinely dries up - companies do not announce on weekends and HN slows."""

SATURDAY = 5
"""`date.weekday()` counts from Monday, so Saturday and Sunday are 5 and 6."""


class SlotState(StrEnum):
    """What became of one planned time. The value is what lands in the database."""

    PLANNED = "planned"
    """Drawn and registered, still ahead of the clock."""

    POSTED = "posted"
    """One curated item went into the group at this slot."""

    SKIPPED = "skipped"
    """Nothing in the pool cleared the quality bar. Rebe never posts filler."""

    DROPPED = "dropped"
    """The slot came and went unusable: deferred past its window, or refused."""

    PRUNED = "pruned"
    """Given up after big news went out on top of the day, per section 4.

    Not the same as `SKIPPED`, and the difference is worth a state of its own: a
    skipped window had nothing worth posting at all, while a pruned one had
    something and it was mediocre, on a day that already had its big moment.
    """


@dataclass(frozen=True, slots=True)
class Slot:
    """One drawn time, and the window edge it must not drift far past."""

    window: str
    at: datetime
    closes: datetime
    state: SlotState = SlotState.PLANNED
    tier: Tier = Tier.NORMAL
    """Which rule put this slot on the day.

    Every drawn slot is normal tier; a high-tier override writes itself into the
    day as an extra slot so that the plan stays the record of what happened, and
    so that a restart can see the day went to five posts rather than four. Section
    4 is explicit that high-tier slots are never pruned.
    """


@dataclass(frozen=True, slots=True)
class DayPlan:
    """Everything Rebe intends to post on one local day, in order."""

    day: date
    slots: tuple[Slot, ...]

    @property
    def pending(self) -> tuple[Slot, ...]:
        """The slots that have not happened yet, earliest first."""
        return tuple(
            sorted(
                (slot for slot in self.slots if slot.state is SlotState.PLANNED),
                key=lambda slot: slot.at,
            )
        )


@dataclass(frozen=True, slots=True)
class Cadence:
    """The posture: which windows, how far apart, and how far a slot may slip.

    Parameters rather than constants because the ramp in section 1 tightens the
    day, and because a test that wants to exercise the gap needs windows that
    make it bind.
    """

    weekday: tuple[PostWindow, ...] = WEEKDAY_WINDOWS
    weekend: tuple[PostWindow, ...] = WEEKEND_WINDOWS
    gap: tuple[timedelta, timedelta] = (timedelta(minutes=75), timedelta(minutes=90))
    """The jittered global minimum between two consecutive planned times."""

    gap_attempts: int = 4
    """How many redraws a badly spaced slot gets before it is given up."""

    defer: tuple[timedelta, timedelta] = (timedelta(minutes=10), timedelta(minutes=20))
    """How long after her last message of any kind a colliding post waits."""

    grace: timedelta = timedelta(minutes=30)
    """How far past its window edge a deferred post may still go out."""

    watch: tuple[timedelta, timedelta] = (timedelta(minutes=20), timedelta(minutes=40))
    """How often the loop looks up from the plan to see whether something broke.

    Jittered like everything else, and free: a look costs one HN query and eight
    feed reads, never a model call. Twenty to forty minutes is the resolution at
    which "she posted it when she saw it" still reads as a person seeing it.
    """

    daily_stop: int = 8
    """The practical stop from section 1, counting posts of both tiers.

    The absolute stop is the anti-ban envelope's twelve sends a day, and the gap
    between the two numbers is the point: this one shapes a normal day, and that
    one exists to catch a runaway loop.
    """

    def __post_init__(self) -> None:
        for windows in (self.weekday, self.weekend):
            names = [window.name for window in windows]
            duplicated = {name for name in names if names.count(name) > 1}
            if duplicated:
                raise ValueError(f"two windows in one day cannot share a name: {duplicated}")
        low, high = self.gap
        if low > high:
            raise ValueError(f"the minimum gap range runs backwards: {low} to {high}")

    def windows_for(self, day: date) -> tuple[PostWindow, ...]:
        """The window set that day belongs to."""
        return self.weekend if day.weekday() >= SATURDAY else self.weekday

    def deadline_for(self, slot: Slot, zone: tzinfo) -> datetime:
        """The first moment a slot is too late to post, so it is dropped instead.

        Section 5 gives a deferred post about thirty minutes past its window edge.
        Section 2 says nothing goes out after 23:00 at all, and that outranks the
        grace: the late window's grace would otherwise run to 23:30, where the
        pacer holds every post anyway. Capping it here rather than learning it
        from a refusal is what stops the day paying DeepSeek to write a post that
        was never going to be allowed out.
        """
        quiet = moment_on(slot.closes.astimezone(zone).date(), WAKING_CLOSES, zone)
        return min(slot.closes + self.grace, quiet)


def moment_on(day: date, moment: time, zone: tzinfo) -> datetime:
    """A wall-clock time on a given day, in the agent's zone."""
    return datetime.combine(day, moment, tzinfo=zone)


def spread(low: timedelta, high: timedelta, fraction: float) -> timedelta:
    """The point `fraction` of the way from `low` to `high`."""
    return low + (high - low) * fraction


def minutes(span: timedelta) -> str:
    """A duration a human reads at a glance, for a log line."""
    total = span.total_seconds()
    if total < 90:
        return f"{total:.0f}s"
    if total < 5400:
        return f"{total / 60:.0f}m"
    return f"{total / 3600:.1f}h"


def jittered_gap(rng: random.Random, cadence: Cadence) -> timedelta:
    """One draw of the global minimum gap, somewhere in the configured range.

    Drawn once per pair of slots rather than once per redraw attempt. Redrawing
    per attempt would hand the plan the *minimum* of its draws, which quietly
    turns "75 to 90 minutes" into a flat 75 and puts back the periodic rhythm the
    jitter existed to remove - the same argument the pacer makes about a caller
    that retries.
    """
    return spread(*cadence.gap, rng.random())


def draw_plan(
    day: date, *, zone: tzinfo, rng: random.Random, cadence: Cadence | None = None
) -> DayPlan:
    """Draw the whole day: one time per window, spaced, clipped, in order.

    A window whose time cannot be spaced far enough from the one before it is
    dropped after `gap_attempts` redraws. That is the soft edge from section 1
    working: the count drifts, and a bunched pair would break the envelope.
    """
    cadence = cadence or Cadence()
    slots: list[Slot] = []
    for window in cadence.windows_for(day):
        opens = moment_on(day, window.opens, zone)
        closes = moment_on(day, window.closes, zone)
        at = _draw_inside(opens, closes, window.span, rng)

        if slots:
            required = jittered_gap(rng, cadence)
            previous = slots[-1].at
            for _ in range(cadence.gap_attempts):
                if at - previous >= required:
                    break
                at = _draw_inside(opens, closes, window.span, rng)
            if at - previous < required:
                logger.info(
                    "dropping the %s slot on %s: nothing in %s clears %s after %s",
                    window.name,
                    day.isoformat(),
                    window,
                    minutes(required),
                    previous.strftime("%H:%M"),
                )
                continue

        slots.append(Slot(window=window.name, at=at, closes=closes))

    plan = DayPlan(day=day, slots=tuple(slots))
    logger.info(
        "rolled %s: %s",
        day.isoformat(),
        ", ".join(f"{slot.window} {slot.at:%H:%M}" for slot in plan.slots) or "nothing",
    )
    return plan


def _draw_inside(
    opens: datetime, closes: datetime, span: timedelta, rng: random.Random
) -> datetime:
    """One Gaussian draw around the window's midpoint, clipped inside its edges.

    Clipped rather than folded back in, which is what section 3 asks for: at two
    and a half sigma the tails are thin enough that piling them onto an edge is
    not a rhythm anybody could see, and an edge is a legitimate time to post.

    The window is half-open, like the pacer's: the last drawable moment is one
    `RESOLUTION` before it closes. That matters at exactly one edge and it is the
    one that counts - a late window closing at 23:00 must not draw 23:00 itself,
    because the overnight hold starts on that second and the pacer would refuse
    the post after the day had already paid DeepSeek to write it.
    """
    middle = opens + span / 2
    sigma = span.total_seconds() / SIGMA_DIVISOR
    drawn = middle + timedelta(seconds=round(rng.gauss(0.0, sigma)))
    return min(max(drawn, opens), closes - RESOLUTION)


def _since_midnight(moment: time) -> timedelta:
    return timedelta(hours=moment.hour, minutes=moment.minute, seconds=moment.second)
