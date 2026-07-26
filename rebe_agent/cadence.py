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
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import StrEnum

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


@dataclass(frozen=True, slots=True)
class Slot:
    """One drawn time, and the window edge it must not drift far past."""

    window: str
    at: datetime
    closes: datetime
    state: SlotState = SlotState.PLANNED


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

    sigma_divisor: float = SIGMA_DIVISOR
    """The window width is divided by this to get the Gaussian's sigma."""

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


def moment_on(day: date, moment: time, zone: tzinfo) -> datetime:
    """A wall-clock time on a given day, in the agent's zone."""
    return datetime.combine(day, moment, tzinfo=zone)


def jittered_gap(rng: random.Random, cadence: Cadence) -> timedelta:
    """One draw of the global minimum gap, somewhere in the configured range.

    Drawn once per pair of slots rather than once per redraw attempt. Redrawing
    per attempt would hand the plan the *minimum* of its draws, which quietly
    turns "75 to 90 minutes" into a flat 75 and puts back the periodic rhythm the
    jitter existed to remove - the same argument the pacer makes about a caller
    that retries.
    """
    low, high = cadence.gap
    return low + (high - low) * rng.random()


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
        at = _draw_inside(opens, closes, window.span, rng, cadence.sigma_divisor)

        if slots:
            required = jittered_gap(rng, cadence)
            previous = slots[-1].at
            for _ in range(cadence.gap_attempts):
                if at - previous >= required:
                    break
                at = _draw_inside(opens, closes, window.span, rng, cadence.sigma_divisor)
            if at - previous < required:
                logger.info(
                    "dropping the %s slot on %s: nothing in %s clears %s after %s",
                    window.name,
                    day.isoformat(),
                    window,
                    _minutes(required),
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
    opens: datetime, closes: datetime, span: timedelta, rng: random.Random, divisor: float
) -> datetime:
    """One Gaussian draw around the window's midpoint, clipped to its edges.

    Clipped rather than folded back in, which is what section 3 asks for: at
    two and a half sigma the tails are thin enough that piling them onto an edge
    is not a rhythm anybody could see, and an edge is a legitimate time to post.
    """
    middle = opens + span / 2
    sigma = span.total_seconds() / divisor
    drawn = middle + timedelta(seconds=round(rng.gauss(0.0, sigma)))
    return min(max(drawn, opens), closes)


def _since_midnight(moment: time) -> timedelta:
    return timedelta(hours=moment.hour, minutes=moment.minute, seconds=moment.second)


def _minutes(span: timedelta) -> str:
    """A duration a human reads at a glance, for a log line."""
    return f"{span.total_seconds() / 60:.0f}m"
