"""Starting quiet, and going quiet again when WhatsApp pushes back.

Section 1 of `docs/wayfinder/anti-ban-ops-spec.md` and the failure table in
section 5 of `docs/wayfinder/deployment-architecture-spec.md` describe the two
moments a bot most obviously looks like one: the first days after a number is
paired, and the minutes after a reconnect. Both get the same answer - send less
- so both live here, as one small piece of state the pacer reads.

**The clamp.** Week one of automation caps the day at three news posts, week two
at four, and after two clean weeks the cadence spec's steady state applies with
no clamp at all. The playbook's own week-two number is five; section 1 of
`docs/wayfinder/posting-cadence-spec.md` puts the target at four and says the
ramp cap of five is not binding, so four is what actually governs and four is
what is written down here. Replies are not clamped: the playbook says "replies
as normal", and somebody answering a question is not somebody filling the day up.

**The ramp start is persisted**, because a ramp a redeploy silently restarted
would hold her at three posts a day forever, and one a redeploy skipped would
front-load a number that is still a fortnight old. It is stamped the first time
anything asks - the agent has no pairing event to hang it on, and first boot
after pairing is the closest honest thing there is.

**Re-entry.** An idle gap of 72 hours or more, or a reconnect, puts her back on
the week-one clamp: a cold resume at full rate is a documented way to trip the
463 reach-out time-lock. The idle gap is measured against the send log rather
than against a flag, so it is a statement about the number rather than about
this process; when there are no sends at all it is measured from the ramp start,
because a number that has never sent anything is the coldest one there is. It
holds for as long as the silence does, so the ramp effectively begins when she
begins talking again rather than when the gap was first noticed.

**The halt.** Two things stop sending outright, and neither is the operator's
soft pause. A 463 or a 429 holds every send for the back-off window, because
continuing to hammer a number WhatsApp is already throttling is the fastest way
to turn a rate limit into a ban. A `connection.update` that says the link is
down holds sending until the link is back. They are counted separately, because
a socket coming back is no evidence at all that WhatsApp has stopped throttling.

Both holds are *bounded*. Evolution repeats a disconnect while it keeps failing,
so a link that is really down keeps extending its own hold, while an `open`
event lost on the way to the webhook costs half an hour of quiet rather than a
silence only a redeploy clears. A link hold that lapses that way is treated as
the reconnect it stands in for, week-one clamp and all: every way of coming back
from a disconnect is a cold resume, and none of them resumes at the old rate.

**Nothing here swaps the instance.** A ban signal stops sending and waits for a
human, per section 4 of the playbook: auto-switching on a possibly-false signal
would burn the only warm standby and leave the bot cold with no backup. Which
instance is live is `EVOLUTION_INSTANCE` in `rebe_agent.config`, and the swap is
the manual procedure in `docs/wayfinder/ramp-and-recovery-runbook.md`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from rebe_agent.clock import Clock
from rebe_agent.db import Pool, open_pool
from rebe_agent.sends import SendLog

logger = logging.getLogger("rebe_agent.ramp")


class RampReason(StrEnum):
    """Why the current ramp began. The value is what lands in the database."""

    PAIRED = "paired"
    """The first ramp there ever was: automation starting on a fresh number."""

    RECONNECTED = "reconnected"
    """Evolution's link came back, and a cold resume at full rate is a ban risk."""

    IDLE = "idle"
    """Nothing went out for 72 hours or more, so the number is cold again."""


class HaltKind(StrEnum):
    """Why sending is stopped. Two shapes, because they read differently."""

    LINK_DOWN = "link_down"
    """Evolution reports the WhatsApp link as down; nothing can get out anyway."""

    BACKING_OFF = "backing_off"
    """WhatsApp pushed back on a send, so the door is deliberately held shut."""


@dataclass(frozen=True, slots=True)
class RampPlan:
    """The numbers the ramp is made of.

    Parameters rather than constants for the same reason the pacer's `Envelope`
    is: a test that wants to exercise one rule needs the others out of its way,
    and these are exactly the numbers a later posture decision moves.
    """

    caps: tuple[int, ...] = (3, 4)
    """Posts a day, one entry per week of the ramp. Past the end there is no cap."""

    week: timedelta = timedelta(days=7)
    """How long each entry in `caps` lasts."""

    idle_gap: timedelta = timedelta(hours=72)
    """The silence that counts as having gone cold, from section 1 of the playbook."""

    back_off: timedelta = timedelta(hours=1)
    """How long a 463 or a 429 stops every send.

    The playbook says "stop sending, back off, alert the maintainer" without
    naming a duration. An hour is the smallest span that is unmistakably a
    back-off rather than a retry: it is a whole turn of the pacer's rolling-hour
    ceiling, so the number goes quiet long enough for WhatsApp to see a pause
    rather than a slower loop. A second push-back restarts it, and the alert is
    what brings a human in if it keeps happening.
    """

    link_hold: timedelta = timedelta(minutes=30)
    """How long a disconnect stops sending when no reconnect is ever seen.

    Evolution re-announces a link that is still down, and each announcement
    extends this, so a real outage stays held for as long as it lasts. The cap
    is here for the other case: an `open` event lost between Evolution and the
    webhook must cost half an hour of quiet, not an indefinite silence that only
    a redeploy clears.
    """

    def __post_init__(self) -> None:
        if self.week <= timedelta(0):
            raise ValueError(f"a ramp week has to be a positive span, got {self.week!r}")
        if any(cap < 1 for cap in self.caps):
            raise ValueError(f"a ramp week that allows no posts at all is a pause: {self.caps}")

    def cap_after(self, elapsed: timedelta) -> int | None:
        """The day's post cap `elapsed` into the ramp, or `None` once it is over."""
        week = max(elapsed, timedelta(0)) // self.week
        return self.caps[week] if week < len(self.caps) else None


@dataclass(frozen=True, slots=True)
class Halt:
    """Sending is stopped, why, and when the door opens again."""

    kind: HaltKind
    """Which of the two things stopped it. The pacer reads this as a refusal reason."""

    detail: str
    """The sentence a maintainer reads in the refusal, in Evolution's own words."""

    until: datetime
    """When it lapses. Both holds are bounded; neither waits for a human."""


@dataclass(frozen=True, slots=True)
class RampState:
    """Where the ramp stands, and whatever is currently holding sending shut.

    One record rather than three, because they are read together on every send
    and a caller that could see a stale part of them would be acting on a state
    that never existed.
    """

    started_at: datetime
    """When the current ramp began. The clamp is measured from here."""

    reason: RampReason = RampReason.PAIRED
    """What put the ramp here, for the log line and for the ops chat."""

    link_down_until: datetime | None = None
    """Set while Evolution says the link is down, and cleared when it comes back.

    It outlives its own deadline on purpose: it is what tells a reconnect worth
    re-entering the ramp for from the `open` Evolution announces at every boot,
    and a reconnect easily arrives after the hold has lapsed.
    """

    link_detail: str = ""
    """What Evolution said, in the words the refusal and the alert will carry."""

    backing_off_until: datetime | None = None
    """Set while WhatsApp is being given room after a 463 or a 429."""

    back_off_detail: str = ""
    """Which push-back it was, in the words the refusal will carry."""


class RampStore(Protocol):
    """Where the ramp lives. One implementation is Postgres; one is a variable."""

    async def state(self) -> RampState | None:
        """The stored ramp, or `None` if this deployment has never had one."""

    async def save(self, state: RampState) -> RampState:
        """Write the ramp down and hand back what is now of record."""


class InMemoryRampStore:
    """A store that forgets on restart. For tests and for local dry runs."""

    def __init__(self) -> None:
        self._state: RampState | None = None

    async def state(self) -> RampState | None:
        return self._state

    async def save(self, state: RampState) -> RampState:
        self._state = state
        return state


class RampGate(Protocol):
    """What the pacer needs from the ramp: may she send, and how many posts today.

    A protocol rather than the class itself, so the pacer holds the two questions
    it actually asks rather than everything a `connection.update` can do to the
    ramp - and so a pacer with no ramp wired is still a pacer.
    """

    async def halt(self) -> Halt | None:
        """Why sending is stopped right now, or `None` to go ahead."""

    async def post_cap(self) -> int | None:
        """How many news posts today may hold, or `None` for the steady state."""


class SteadyState:
    """The ramp a pacer gets when nobody wired one in.

    A `--say` from the command line, a dry run, or a test about the envelope has
    no ramp behind it. Read-only on purpose, like `NeverPaused`: something that
    answered "back off" by doing nothing would be a back-off that lies.
    """

    async def halt(self) -> Halt | None:
        return None

    async def post_cap(self) -> int | None:
        return None


class Ramp:
    """The post-pairing ramp and the back-off, over one persisted record."""

    def __init__(
        self,
        store: RampStore,
        clock: Clock,
        sends: SendLog,
        *,
        plan: RampPlan | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._sends = sends
        self._plan = plan or RampPlan()

    async def state(self) -> RampState:
        """Where the ramp stands, re-entering it if the number has gone cold.

        Both re-entry rules are checked here rather than on a timer, because
        every caller that cares - the pacer about to send, the boot line, a test
        - has to see the same answer, and a rule that only applied when something
        happened to look would be a rule about the looking. This does write, and
        the write is the point: re-entering the ramp is what "where does it
        stand" sometimes means.
        """
        now = self._clock.now()
        current = await self._store.state()
        if current is None:
            return await self._start(RampState(started_at=now))

        current = await self._resume_if_the_hold_lapsed(now, current)
        return await self._re_enter_if_she_has_gone_quiet(now, current)

    async def _resume_if_the_hold_lapsed(self, now: datetime, current: RampState) -> RampState:
        """Come back from a disconnect nobody ever announced the end of.

        Evolution reconnects on its own and says so, and `link_up` is the usual
        way this hold clears. The `open` can also be lost between Evolution and
        the webhook, and half an hour later the link is almost certainly back -
        so the hold lapsing is treated as the reconnect it stands in for, clamp
        and all. Resuming at the previous rate instead would be exactly the cold
        resume section 4 of the playbook warns about.

        This is also how a waited-out temporary ban comes back on the ramp: the
        ban arrives as a disconnect, the operator resumes hours later, and by
        then the hold has long since lapsed into a re-entry.
        """
        if current.link_down_until is None or now < current.link_down_until:
            return current
        logger.info("the link hold lapsed with no reconnect announced; treating it as one")
        return await self._start(
            replace(
                current,
                started_at=now,
                reason=RampReason.RECONNECTED,
                link_down_until=None,
                link_detail="",
            )
        )

    async def _re_enter_if_she_has_gone_quiet(self, now: datetime, current: RampState) -> RampState:
        """Put her back on the week-one clamp while nothing has gone out for 72 hours.

        Re-stamped on every look for as long as the silence lasts, rather than
        once when it is noticed. Stamping it once would let the ramp age through
        the rest of the gap, and a fortnight of quiet would end with the number
        back at full rate - which is the cold resume the rule exists to prevent.
        Re-stamping means the ramp effectively starts when she starts talking
        again, and then runs its full two weeks from there.
        """
        latest = await self._sends.latest()
        # With no sends at all the gap is measured from the ramp itself: a number
        # that has never sent anything is the coldest one there is, and it must
        # not age its way into the steady state by sitting still.
        anchor = latest.sent_at if latest is not None else current.started_at
        if now - anchor < self._plan.idle_gap:
            return current

        if current.started_at <= anchor:
            # The first look of this gap. The rest of them re-stamp in silence,
            # because one silence is one piece of news.
            logger.info(
                "nothing has gone out since %s, which is longer than %s; back on the "
                "week-one clamp",
                anchor.isoformat(timespec="seconds"),
                self._plan.idle_gap,
            )
        return await self._store.save(replace(current, started_at=now, reason=RampReason.IDLE))

    async def post_cap(self) -> int | None:
        """How many news posts today may hold, or `None` once the ramp is over."""
        state = await self.state()
        return self._plan.cap_after(self._clock.now() - state.started_at)

    async def halt(self) -> Halt | None:
        """Why sending is stopped right now, or `None` to go ahead.

        When both holds are live the longer one is the answer, so the refusal a
        caller sees carries the moment sending can actually resume rather than
        the first of two doors to open.
        """
        state = await self.state()
        now = self._clock.now()
        live = [
            halt
            for halt in (
                _hold(HaltKind.LINK_DOWN, state.link_detail, state.link_down_until),
                _hold(HaltKind.BACKING_OFF, state.back_off_detail, state.backing_off_until),
            )
            if halt is not None and now < halt.until
        ]
        return max(live, key=lambda halt: halt.until, default=None)

    async def link_down(self, detail: str) -> None:
        """Evolution says the link is down: nothing goes out until it is back."""
        state = await self.state()
        logger.warning("sending is stopped: %s", detail)
        await self._store.save(
            replace(
                state,
                link_down_until=self._clock.now() + self._plan.link_hold,
                link_detail=detail,
            )
        )

    async def link_up(self) -> None:
        """The link is back: sending resumes, on the week-one clamp.

        Only when the link was seen to go down. Evolution announces an open
        connection at every boot too, and treating that as a reconnect would put
        a redeployed agent back on week one every time it started.

        A back-off is deliberately left where it is. WhatsApp throttling the
        number and Evolution's socket dropping are two different pieces of news,
        and neither one clears the other.
        """
        state = await self.state()
        if state.link_down_until is None:
            return
        logger.info("the link is back")
        await self._start(
            replace(
                state,
                started_at=self._clock.now(),
                reason=RampReason.RECONNECTED,
                link_down_until=None,
                link_detail="",
            )
        )

    async def back_off(self, detail: str) -> None:
        """WhatsApp pushed back on a send: hold every send for the back-off window."""
        state = await self.state()
        logger.warning("backing off for %s: %s", self._plan.back_off, detail)
        await self._store.save(
            replace(
                state,
                backing_off_until=self._clock.now() + self._plan.back_off,
                back_off_detail=detail,
            )
        )

    async def _start(self, state: RampState) -> RampState:
        """Write down a ramp that starts now, and say so once where it is readable."""
        logger.info(
            "the ramp starts at %s (%s): %s",
            state.started_at.isoformat(timespec="seconds"),
            state.reason,
            _describe(self._plan),
        )
        return await self._store.save(state)


def _hold(kind: HaltKind, detail: str, until: datetime | None) -> Halt | None:
    """One of the two holds as a `Halt`, or `None` if it was never set."""
    return None if until is None else Halt(kind=kind, detail=detail, until=until)


def _describe(plan: RampPlan) -> str:
    """The clamp a human reads at a glance, for the one log line that says it."""
    weeks = ", ".join(f"week {number + 1} {cap} posts/day" for number, cap in enumerate(plan.caps))
    return f"{weeks}, then the steady state"


SCHEMA = """
CREATE TABLE IF NOT EXISTS ramp (
    id                smallint    PRIMARY KEY,
    started_at        timestamptz NOT NULL,
    reason            text        NOT NULL,
    link_down_until   timestamptz,
    link_detail       text        NOT NULL DEFAULT '',
    backing_off_until timestamptz,
    back_off_detail   text        NOT NULL DEFAULT ''
)
"""

ONLY_ROW = 1
"""There is one ramp. Its row is pinned to one id so there cannot be two."""

COLUMNS = "started_at, reason, link_down_until, link_detail, backing_off_until, back_off_detail"


class PostgresRampStore:
    """The real store: one row in the `rebe` database from the deployment spec.

    One row, because the ramp is a property of the *number* rather than of a
    process - which is the whole point of putting it here instead of in memory.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        self._table_is_there = False

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str) -> AsyncIterator[PostgresRampStore]:
        """Open a small pool, make sure the table exists, and hand back a store."""
        async with open_pool(database_url) as pool:
            store = cls(pool)
            await store.ensure_schema()
            yield store

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)
        self._table_is_there = True

    async def _ready(self) -> None:
        """Create the table on first use, and try again next time if that failed.

        The same forgiveness the soft pause has, and for the same reason: the
        `rebe` database can be a few seconds behind the container, and a store
        that missed its one chance at boot would leave the ramp unreadable -
        which is the state in which Rebe posts as if she were a month old.
        """
        if not self._table_is_there:
            await self.ensure_schema()

    async def state(self) -> RampState | None:
        await self._ready()
        async with self._pool.connection() as conn:
            cursor = await conn.execute(f"SELECT {COLUMNS} FROM ramp WHERE id = %s", (ONLY_ROW,))
            row = await cursor.fetchone()
        return _row_to_state(row) if row is not None else None

    async def save(self, state: RampState) -> RampState:
        await self._ready()
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                INSERT INTO ramp (id, {COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    started_at        = EXCLUDED.started_at,
                    reason            = EXCLUDED.reason,
                    link_down_until   = EXCLUDED.link_down_until,
                    link_detail       = EXCLUDED.link_detail,
                    backing_off_until = EXCLUDED.backing_off_until,
                    back_off_detail   = EXCLUDED.back_off_detail
                RETURNING {COLUMNS}
                """,
                (
                    ONLY_ROW,
                    state.started_at,
                    str(state.reason),
                    state.link_down_until,
                    state.link_detail,
                    state.backing_off_until,
                    state.back_off_detail,
                ),
            )
            row = await cursor.fetchone()
        assert row is not None, "an upsert with RETURNING always answers with its row"
        return _row_to_state(row)


def _row_to_state(row: tuple[object, ...]) -> RampState:
    started_at, reason, link_down_until, link_detail, backing_off_until, back_off_detail = row
    assert isinstance(started_at, datetime)
    assert link_down_until is None or isinstance(link_down_until, datetime)
    assert backing_off_until is None or isinstance(backing_off_until, datetime)
    return RampState(
        started_at=started_at,
        reason=RampReason(str(reason)),
        link_down_until=link_down_until,
        link_detail=str(link_detail),
        backing_off_until=backing_off_until,
        back_off_detail=str(back_off_detail),
    )
