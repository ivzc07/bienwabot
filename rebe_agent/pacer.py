"""The shared anti-ban pacer: every message Rebe sends leaves through here.

Section 2.2 of `docs/wayfinder/deployment-architecture-spec.md` puts one limiter
in one process for a reason worth restating: the ceilings span posts *and*
replies. Two limiters, one per leg, would each stay politely under twelve a day
and together send twenty-four. So the news leg and the webhook leg call the same
`Pacer` object, and it is the only thing in the codebase that calls Evolution's
send endpoint.

Two different jobs live here, and they answer differently.

**Looking human**, from section 2 of `docs/wayfinder/anti-ban-ops-spec.md`. A
`composing` presence goes up, a pause scaled to the message length passes -
about 30 ms a character, Gaussian-jittered, never below 1.5 s or above 5 s - the
presence is refreshed while it passes because Baileys expires it after about ten
seconds, and only then does the text go out. A first message into a quiet thread
gets an extra beat before any of that. These are *waits*, all of them bounded by
seconds. Nobody is told about them.

**Staying under the ceilings**, which is where a caller can be told no. Four
sends a minute, three an hour, twelve a day, counted across both legs; scheduled
posts held between 23:00 and 08:00; consecutive posts 75-90 minutes apart; sends
between 02:00 and 06:00 spaced four to six times further apart than by day; and
never the same wording twice in a row. The per-minute floor is a *wait*, because
a full minute window drains in under a minute. Everything else is a
`SendRefusedError` carrying a `RefusalReason` and, where it is knowable, how long
until the door opens - because the choice between deferring a post and dropping
it belongs to the cadence ticket, not here.

Every jittered gap is drawn from the send it is measured against rather than
freshly per attempt. Redrawing would hand a caller that retries the *minimum* of
its draws, which quietly turns "75 to 90 minutes" into a flat 75 and puts the
periodic rhythm back that the jitter existed to remove.

Two ceilings that both bind at once are worth naming: with three sends an hour,
the four-a-minute floor can never be reached in production. It is still enforced,
because it is the last-resort burst guard if the hourly ceiling is ever raised,
and because the ramp in section 1 of the playbook exists to move these numbers.

A `SendRefusedError` is not an `EvolutionError`. One means the envelope said no
and the message can be tried later; the other means the transport is broken. A
caller that cannot tell them apart cannot do the right thing with either.

Two seams hang off the ops channel of ticket #23, and both are here rather than
in either leg for the same reason the ceilings are: this is the one place a
message leaves through. The out-of-band soft pause is read before every send, so
one switch silences posts and replies together; and a send that fails in
transport is reported to a `SendWatch`, because a 463 or a temporary ban is only
ever seen right here.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import TypeVar

from rebe_agent.clock import Clock, RealSleeper, Sleeper
from rebe_agent.evolution import COMPOSING, PAUSED, EvolutionError, EvolutionSender
from rebe_agent.pause import NeverPaused, Pause
from rebe_agent.sends import SendKind, SendLog, SendRecord, fingerprint, stable_fraction
from rebe_agent.signals import SendWatch, Watchtower

logger = logging.getLogger("rebe_agent.pacer")

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)

QUIET_THREAD_AFTER = timedelta(minutes=30)
"""How long a chat must have been silent for the next message to open a thread."""

_EPSILON = 1e-6
"""Below this many seconds a wait is floating-point noise, not a wait."""

_Spreadable = TypeVar("_Spreadable", timedelta, float)
"""What a jittered range can be made of: a span of time, or a plain multiplier."""


class RefusalReason(StrEnum):
    """Why the envelope said no. The caller decides between deferring and dropping."""

    SOFT_PAUSE = "soft_pause"
    """The out-of-band soft pause is on. Nothing goes out until a human flips it."""

    DUPLICATE = "duplicate"
    """The previous message had identical wording."""

    OVERNIGHT_HOLD = "overnight_hold"
    """A scheduled post came due between 23:00 and 08:00 local."""

    NIGHT_HUSH = "night_hush"
    """Too soon after the previous send, at an hour when Rebe is near-silent."""

    MINIMUM_GAP = "minimum_gap"
    """Too soon after the previous post."""

    HOURLY_CEILING = "hourly_ceiling"
    """The rolling hour is full, counting posts and replies together."""

    DAILY_CEILING = "daily_ceiling"
    """The local day is full, counting posts and replies together."""


class SendRefusedError(RuntimeError):
    """The envelope refused the send. Nothing was sent, and nothing is broken."""

    def __init__(
        self, reason: RefusalReason, detail: str, *, retry_after: timedelta | None = None
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.retry_after = retry_after
        when = f" Try again in {_minutes(retry_after)}." if retry_after is not None else ""
        super().__init__(f"{reason}: {detail}.{when}")


@dataclass(frozen=True, slots=True)
class Window:
    """A wall-clock window, which may wrap past midnight."""

    opens: time
    closes: time

    def contains(self, moment: time) -> bool:
        if self.opens <= self.closes:
            return self.opens <= moment < self.closes
        return moment >= self.opens or moment < self.closes

    def __str__(self) -> str:
        return f"{self.opens:%H:%M}-{self.closes:%H:%M}"


@dataclass(frozen=True, slots=True)
class Envelope:
    """The ceilings and windows from section 2 of the anti-ban playbook.

    Defaults are the shipped balanced posture. They are parameters rather than
    constants because the post-pairing ramp tightens them for the first two weeks
    of automation, and because a test that wants to exercise one rule needs the
    others out of its way.
    """

    sends_per_minute: int = 4
    sends_per_hour: int = 3
    sends_per_day: int = 12
    post_gap: tuple[timedelta, timedelta] = (timedelta(minutes=75), timedelta(minutes=90))
    overnight_hold: Window = Window(time(23, 0), time(8, 0))
    night_hush: Window = Window(time(2, 0), time(6, 0))
    night_hush_slowdown: tuple[float, float] = (4.0, 6.0)

    @property
    def spacing(self) -> timedelta:
        """How far apart two sends normally sit: the hourly ceiling, spread out.

        The playbook asks for "slow 4-6x" overnight without naming what the 1x
        is. This is it - three an hour is one every twenty minutes - so the hush
        is a real rate change rather than a longer pause that changes nothing.
        """
        return HOUR / self.sends_per_hour


@dataclass(frozen=True, slots=True)
class TypingProfile:
    """How long Rebe appears to type, and how that pause is drawn.

    `maximum_ms` sits below `presence_refresh_seconds` in the shipped numbers, so
    with the posture as it stands the refresh never fires: no message is slow
    enough to type. It is written as a loop anyway, because the clamp is a
    posture decision a ramp or a later spec may widen, while presence expiring
    after about ten seconds is a property of Baileys that will not move with it.
    """

    ms_per_char: float = 30.0
    jitter_ratio: float = 0.25
    minimum_ms: float = 1500.0
    maximum_ms: float = 5000.0
    quiet_thread_ms: float = 3000.0
    presence_refresh_seconds: float = 8.0


@dataclass(frozen=True, slots=True)
class SentMessage:
    """What the caller gets back once the message has landed."""

    message_id: str
    at: datetime
    typing_seconds: float
    waited_seconds: float
    """Everything spent before typing began: the minute floor, the opening beat."""


class Pacer:
    """One paced, counted, human-looking send. Both legs share one instance."""

    def __init__(
        self,
        client: EvolutionSender,
        log: SendLog,
        clock: Clock,
        *,
        envelope: Envelope | None = None,
        typing: TypingProfile | None = None,
        sleeper: Sleeper | None = None,
        rng: random.Random | None = None,
        pause: Pause | None = None,
        watch: SendWatch | None = None,
    ) -> None:
        self._client = client
        self._log = log
        self._clock = clock
        self._envelope = envelope or Envelope()
        self._typing = typing or TypingProfile()
        self._sleeper = sleeper or RealSleeper()
        self._rng = rng or random.Random()
        self._pause = pause or NeverPaused()
        self._watch = watch or Watchtower()
        # Held for the whole send, typing pause included. Two things fall out of
        # that: the ceilings cannot be read by two callers at once and both act
        # on the same number, and Rebe is never typing two messages at the same
        # moment - which is the burst the envelope exists to prevent.
        self._turnstile = asyncio.Lock()

    async def send(self, kind: SendKind, chat: str, text: str) -> SentMessage:
        """Send one message into `chat`, paced, or refuse and say why.

        Raises `SendRefusedError` when the envelope will not have it, and
        `EvolutionError` when the transport will not have it.
        """
        if not text.strip():
            raise ValueError("refusing to send an empty message")

        async with self._turnstile:
            # The soft pause first, and before anything is waited on: an operator
            # who asked for silence gets it now, not after a minute-long hold,
            # and the group never sees a typing indicator for a message that is
            # not coming.
            await self._check_the_pause()

            # The minute floor next, so every rule below is judged on the clock
            # as it will be when the message actually goes out. A wait of up to
            # a minute can cross 23:00 or a local midnight, and a decision made
            # on the time before the wait would be a decision about the past.
            waited = await self._wait_for_the_minute_floor()

            now = self._clock.now()
            await self._check_ceilings(kind, text, now)
            waited += await self._settle_in(chat, now)

            typing_seconds = self._draw_typing_seconds(text)
            await self._type_for(chat, typing_seconds)
            return await self._deliver(kind, chat, text, waited, typing_seconds)

    async def paused(self) -> bool:
        """Whether the switch says Rebe is meant to be silent right now.

        A leg that has something to do *before* it sends - the reply leg marks
        the message read - asks here rather than holding a switch of its own.
        There is one switch, and it reaches every leg by reaching the one sender,
        so a leg cannot be wired to a pacer that is paused and a switch that is
        not. The send path reads it again anyway: this is an early out, not the
        guarantee.
        """
        return (await self._pause.state()).paused

    async def _check_the_pause(self) -> None:
        """Refuse everything while the out-of-band switch is on.

        Read on every send rather than once at boot, because the point of the
        switch is to silence a process that is already running. Nothing is
        queued: a refusal here means the message is dropped, so unpausing resumes
        normal behaviour instead of firing a backlog at the group.
        """
        state = await self._pause.state()
        if state.paused:
            raise SendRefusedError(
                RefusalReason.SOFT_PAUSE,
                f"the soft pause is on{f' ({state.reason})' if state.reason else ''}; "
                f"nothing goes out until an operator flips it back",
            )

    async def _check_ceilings(self, kind: SendKind, text: str, now: datetime) -> None:
        """Every rule that can answer "no". Raises, or returns having said nothing."""
        previous = await self._log.latest()
        if previous is not None and previous.fingerprint == fingerprint(text):
            raise SendRefusedError(
                RefusalReason.DUPLICATE,
                "the previous message Rebe sent had identical wording",
            )

        if kind is SendKind.POST:
            await self._check_post_rules(now)

        if previous is not None and self._envelope.night_hush.contains(now.time()):
            self._check_the_hush(previous, now)

        recent = await self._log.since(now - HOUR)
        if len(recent) >= self._envelope.sends_per_hour:
            raise SendRefusedError(
                RefusalReason.HOURLY_CEILING,
                f"{len(recent)} sends in the last hour across both legs, "
                f"ceiling is {self._envelope.sends_per_hour}",
                retry_after=recent[0].sent_at + HOUR - now,
            )

        day = self._local_day(now)
        today = await self._log.count_on(day)
        if today >= self._envelope.sends_per_day:
            raise SendRefusedError(
                RefusalReason.DAILY_CEILING,
                f"{today} sends on {day.isoformat()} across both legs, "
                f"ceiling is {self._envelope.sends_per_day}",
                retry_after=_until(now, time(0, 0)),
            )

    async def _check_post_rules(self, now: datetime) -> None:
        """What only the news leg obeys: the overnight hold and the post-to-post gap.

        A directly-addressed reply is exempt from both. Section 2 of the cadence
        spec is explicit that replies may still fire overnight; what keeps them
        rare at that hour is the hush, which both legs obey.
        """
        hold = self._envelope.overnight_hold
        if hold.contains(now.time()):
            raise SendRefusedError(
                RefusalReason.OVERNIGHT_HOLD,
                f"scheduled posts are held {hold} local time; it is {now:%H:%M}",
                retry_after=_until(now, hold.closes),
            )

        last_post = await self._log.latest(kind=SendKind.POST)
        if last_post is None:
            return
        low, high = self._envelope.post_gap
        required = _spread(low, high, stable_fraction(last_post))
        elapsed = now - last_post.sent_at
        if elapsed < required:
            raise SendRefusedError(
                RefusalReason.MINIMUM_GAP,
                f"the last post was {_minutes(elapsed)} ago and this one wants "
                f"{_minutes(required)} of space",
                retry_after=required - elapsed,
            )

    def _check_the_hush(self, previous: SendRecord, now: datetime) -> None:
        """Between 02:00 and 06:00, everything Rebe sends is four to six times rarer.

        The playbook calls this band near-silent. Scheduled posts are already
        held right through it by the wider overnight hold, so in practice this
        governs the addressed replies the reply policy still allows at 03:00: one
        answer goes out, and the next is an hour and a half away rather than
        twenty minutes.
        """
        band = self._envelope.night_hush
        low, high = self._envelope.night_hush_slowdown
        required = self._envelope.spacing * _spread(low, high, stable_fraction(previous))
        elapsed = now - previous.sent_at
        if elapsed < required:
            raise SendRefusedError(
                RefusalReason.NIGHT_HUSH,
                f"Rebe is near-silent {band} local time; the last send was "
                f"{_minutes(elapsed)} ago and sends are {_minutes(required)} apart here",
                retry_after=required - elapsed,
            )

    async def _wait_for_the_minute_floor(self) -> float:
        """Wait, if need be, until the rolling minute has room.

        A full minute window is empty again within a minute, so this is a pause
        rather than a refusal - which is also what stops the absolute burst guard
        from silently eating messages the hourly ceiling would have allowed.
        """
        now = self._clock.now()
        window = list(await self._log.since(now - MINUTE))
        allowed = self._envelope.sends_per_minute
        if len(window) < allowed:
            return 0.0

        oldest_that_must_expire = window[len(window) - allowed]
        seconds = max((oldest_that_must_expire.sent_at + MINUTE - now).total_seconds(), 0.0)
        if seconds > _EPSILON:
            logger.debug("holding %.1fs for the per-minute floor", seconds)
            await self._sleeper.sleep(seconds)
        return seconds

    async def _settle_in(self, chat: str, now: datetime) -> float:
        """The extra beat before the first message into a thread that has gone quiet."""
        last_here = await self._log.latest(chat=chat)
        if last_here is not None and now - last_here.sent_at < QUIET_THREAD_AFTER:
            return 0.0

        seconds = self._jittered(self._typing.quiet_thread_ms) / 1000
        if seconds > _EPSILON:
            logger.debug("pausing %.1fs before opening a quiet thread", seconds)
            await self._sleeper.sleep(seconds)
        return seconds

    async def _type_for(self, chat: str, seconds: float) -> None:
        """Show `composing`, hold it for `seconds`, refreshing before it expires."""
        await self._client.send_presence(chat, COMPOSING)
        remaining = seconds
        while remaining > _EPSILON:
            step = min(remaining, self._typing.presence_refresh_seconds)
            await self._sleeper.sleep(step)
            remaining -= step
            if remaining > _EPSILON:
                await self._client.send_presence(chat, COMPOSING)

    async def _deliver(
        self, kind: SendKind, chat: str, text: str, waited: float, typing_seconds: float
    ) -> SentMessage:
        """Write the send down, then put it on the wire.

        In that order. A send recorded and then lost costs one slot out of twelve;
        a send made and then not recorded is invisible to every ceiling, and a
        transport that is failing is exactly when a caller retries.
        """
        at = self._clock.now()
        await self._log.record(
            SendRecord(
                sent_at=at,
                day=self._local_day(at),
                kind=kind,
                chat=chat,
                fingerprint=fingerprint(text),
            )
        )
        try:
            message_id = await self._client.send_text(chat, text)
        except EvolutionError as exc:
            # A 463 reach-out time-lock, a temp ban, an Evolution that is down:
            # section 4 of the playbook answers all of them with "back off and
            # tell the maintainer", and this is the only place a send can fail.
            await self._watch.send_failed(exc)
            raise
        await self._settle(chat)
        logger.info(
            "sent a %s to %s after %.1fs typing (%.1fs paced), message id %s",
            kind,
            chat,
            typing_seconds,
            waited,
            message_id or "unknown",
        )
        return SentMessage(
            message_id=message_id,
            at=at,
            typing_seconds=typing_seconds,
            waited_seconds=waited,
        )

    async def _settle(self, chat: str) -> None:
        """Stop looking like she is still typing. Never fails the send.

        The message has already landed by now, so an error here is cosmetic; a
        caller told the send failed would be told something untrue.
        """
        try:
            await self._client.send_presence(chat, PAUSED)
        except EvolutionError as exc:
            logger.warning("could not clear the typing presence after the send: %s", exc)

    def _draw_typing_seconds(self, text: str) -> float:
        """A length-scaled, Gaussian-jittered pause, inside the clamp, never constant.

        Out-of-band samples are *reflected* back into the band rather than
        clipped. Clipping would pile every short message onto exactly 1500 ms,
        which is the constant delay the playbook names as a fingerprint; folding
        keeps the pause varying even where the length alone would sit on the edge.

        Unlike the gaps above, this is drawn fresh every time on purpose: it is
        not a threshold a caller can retry against, it is how long she types.
        """
        profile = self._typing
        scaled = len(text) * profile.ms_per_char
        centre = min(max(scaled, profile.minimum_ms), profile.maximum_ms)
        sample = self._rng.gauss(centre, centre * profile.jitter_ratio)
        return _reflect(sample, profile.minimum_ms, profile.maximum_ms) / 1000

    def _jittered(self, milliseconds: float) -> float:
        """A beat around `milliseconds`, never negative."""
        return max(self._rng.gauss(milliseconds, milliseconds * self._typing.jitter_ratio), 0.0)

    def _local_day(self, moment: datetime) -> date:
        """The day in the agent's zone. "Twelve a day" is about the group's day."""
        return moment.astimezone(self._clock.zone).date()


def _spread(low: _Spreadable, high: _Spreadable, fraction: float) -> _Spreadable:
    """The point `fraction` of the way from `low` to `high`."""
    return low + (high - low) * fraction


def _until(now: datetime, moment: time) -> timedelta:
    """How long from `now` until the next local `moment`."""
    target = now.replace(hour=moment.hour, minute=moment.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target - now


def _reflect(value: float, low: float, high: float) -> float:
    """Fold `value` back inside `[low, high]` instead of flattening it onto an edge."""
    if high <= low:
        return low
    folded = value
    while folded < low or folded > high:
        folded = low + (low - folded) if folded < low else high - (folded - high)
    return folded


def _minutes(span: timedelta) -> str:
    """A duration a human reads at a glance, for a log line or a refusal."""
    total = span.total_seconds()
    if total < 90:
        return f"{total:.0f}s"
    return f"{total / 60:.0f}m"
