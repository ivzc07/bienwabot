"""What is worth waking a human for, and what the message has to tell them.

`Signal` is a closed set, taken from section 4 of `docs/wayfinder/anti-ban-ops-spec.md`
(what a flagged number looks like) and the failure table in section 5 of the
deployment spec (what to do about each one). Each signal carries the sentence a
maintainer needs at 2 a.m.: what happened, whether Rebe is still sending, and what
to do next. "Alert" and "act on it" are the same message or the alert is noise.

Two of those signals - the ban shapes - also *stop* Rebe, by flipping the same
soft-pause switch an operator uses, because "stop sending, back off, do not keep
hammering" needs a mechanism and that is the one there is. Coming back is a human
decision: the temporary shape wants waiting out, the permanent one wants the
backup number, and getting those two the wrong way round either burns the only
warm standby or leaves Rebe dead for good.

Three of them also move the *ramp* in `rebe_agent.ramp`, which is the other half
of the same answer: a 463 or a 429 backs sending off rather than retrying it, a
link that has gone down stops sending until it is back, and a link that comes
back resumes on the week-one clamp instead of at the previous rate. That is
section 5 of the deployment spec read literally, and it is deliberately not the
soft pause: the pause is the operator's, and a signal that flipped it would need
an automatic unflip, which is how a human's deliberate "cool it for a bit" gets
silently undone by a reconnect.

`Watchtower` is where an observation becomes a signal, and the seams it satisfies
(`SendWatch`, `BrainWatch`, `LinkWatch`) are how the pacer, the brain and the
webhook leg report without knowing any of this. Nothing here raises: those hooks
are called from inside an `except` block or from a background task, so an
exception here would replace the failure being handled with one about the telling
of it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Protocol

from rebe_agent.alerts import Alerter, LoggingAlerter
from rebe_agent.evolution import EvolutionError, EvolutionRateLimitedError
from rebe_agent.pause import PauseSwitch
from rebe_agent.ramp import Ramp

logger = logging.getLogger("rebe_agent.signals")


class Signal(StrEnum):
    """What is worth telling a human about, out of band. A closed set on purpose.

    The value appears in the alert, so these strings are read by people. The
    sentence each one earns is in `HEADLINES`, so there is one place to reword it.
    """

    RATE_LIMITED = "rate_limited"
    """WhatsApp pushed back on a send."""

    SEND_FAILED = "send_failed"
    """Evolution would not take a message, for some other reason."""

    DISCONNECTED = "disconnected"
    """Evolution reports the WhatsApp link as down."""

    TEMPORARY_BAN = "temporary_ban"
    """The session was ended by WhatsApp."""

    PERMANENT_BAN = "permanent_ban"
    """WhatsApp refuses the number outright."""

    BRAIN_ERROR = "brain_error"
    """A DeepSeek call came back with nothing usable."""


HEADLINES: Mapping[Signal, str] = {
    Signal.RATE_LIMITED: (
        "WhatsApp is rate-limiting Rebe's sends - a 463 reach-out time-lock or a 429. "
        "Nothing is being retried. If it keeps up, pause her from the ops chat for a "
        "few hours."
    ),
    Signal.SEND_FAILED: (
        "Evolution would not take a message, and it was not a rate limit. "
        "Nothing is being retried. Check that bien-evo is up and still paired."
    ),
    Signal.DISCONNECTED: (
        "Evolution reports the WhatsApp link as down, so nothing can go out. "
        "Evolution reconnects on its own; if it does not come back, the number "
        "needs re-pairing (one QR scan). The heartbeat keeps flowing meanwhile."
    ),
    Signal.TEMPORARY_BAN: (
        "WhatsApp ended Rebe's session. That is what a temporary ban looks like "
        "from here, and also what unlinking the device looks like. Check the number "
        "on the phone, wait it out, then resume her from the ops chat. Do not swap "
        "to the backup number for this."
    ),
    Signal.PERMANENT_BAN: (
        "WhatsApp refused Rebe's number outright, which is what a permanent ban "
        "looks like. Set EVOLUTION_INSTANCE=bien-backup and redeploy to post from "
        "the warm standby, then start warming a replacement SIM as the new backup."
    ),
    Signal.BRAIN_ERROR: (
        "A DeepSeek call failed. The group saw nothing - the item was dropped "
        "rather than posted half-written. If it repeats, check the API key and "
        "DeepSeek's own status."
    ),
}
"""One sentence of diagnosis and one of instruction, per signal.

Deliberately free of literal command names: the ops chat answers any unrecognised
message with the verbs it takes, so this prose does not have to be kept in step
with them.
"""

STOPS_SENDING = frozenset({Signal.TEMPORARY_BAN, Signal.PERMANENT_BAN})
"""The signals that also silence Rebe until a human decides otherwise."""

PAUSED_NOTE = "Rebe is now paused and will send nothing until you resume her from this chat."

DISCONNECTED_STATES = frozenset({"close", "closed", "disconnected"})
"""Evolution's `connection.update` states that mean the link is not usable."""

CONNECTED_STATES = frozenset({"open", "connected"})
"""The states that mean the link is usable again.

Named rather than inferred from "not disconnected", because Baileys also reports
`connecting`, and a half-open socket is neither a reason to stop sending nor a
reconnect to come back from.
"""

LOGGED_OUT = 401
FORBIDDEN = 403

BAN_REASONS: Mapping[int, Signal] = {
    LOGGED_OUT: Signal.TEMPORARY_BAN,
    FORBIDDEN: Signal.PERMANENT_BAN,
}
"""Baileys' disconnect reasons that mean more than "the socket dropped".

WhatsApp publishes no ban-reason detail, so this is the best reading available
rather than a documented mapping, and it is one place to correct: `loggedOut`
(401) is the shape a temporary ban arrives in - and equally what pulling the
linked device does - while `forbidden` (403) is WhatsApp refusing the number.
The alert for each says as much, so a human confirms before swapping anything.
"""


class BrainWatch(Protocol):
    """Told when DeepSeek gave no usable answer, so somebody hears what the group
    deliberately does not: section 5 of the deployment spec drops the item
    silently to the group and alerts the maintainer instead."""

    async def brain_failed(self, error: BaseException) -> None:
        """Report one failed call. Never raises."""


class SendWatch(Protocol):
    """Told when a message did not get out, so the signal reaches a human.

    The pacer's seam. It is a protocol rather than the concrete `Watchtower`
    because the pacer has no business knowing what happens to the news - only
    that somebody is listening.
    """

    async def send_failed(self, error: EvolutionError) -> None:
        """Report one failed send. Never raises: the caller has its own problem."""


class LinkWatch(Protocol):
    """Told what Evolution says about the WhatsApp link.

    The webhook leg's seam, for the `connection.update` event the deployment spec
    has each instance subscribe to. A protocol for the same reason `SendWatch` is:
    the leg that receives the delivery has no business knowing what a disconnect
    does to the ramp, only that somebody is listening.
    """

    async def connection_changed(self, state: str, *, reason: int | None = None) -> None:
        """Report one connection state change. Never raises."""


class Watchtower:
    """Turns what the agent observed into one out-of-band alert a human can act on.

    Deliberately not a classifier over free text: every entry point takes a typed
    signal or a typed error, because "is this a ban?" is a decision worth reading
    in one place rather than guessing at from a log line.
    """

    def __init__(
        self,
        alerter: Alerter | None = None,
        *,
        pause: PauseSwitch | None = None,
        ramp: Ramp | None = None,
    ) -> None:
        self._alerter = alerter or LoggingAlerter()
        self._pause = pause
        self._ramp = ramp

    async def send_failed(self, error: EvolutionError) -> None:
        """The pacer's hook: a message did not get out.

        A 463 or a 429 is the ban-adjacent signal the playbook cares about; every
        other transport failure is still worth one throttled line, because a bot
        that cannot send and says nothing looks exactly like a bot with nothing
        to say.

        Only the rate limit backs sending off. A 500 from Evolution is a broken
        hop rather than WhatsApp pushing back, and holding the day shut over one
        would be an outage the playbook never asked for.
        """
        rate_limited = isinstance(error, EvolutionRateLimitedError)
        if rate_limited:
            await self._tell_the_ramp("back off", lambda ramp: ramp.back_off(str(error)))
        await self.report(
            Signal.RATE_LIMITED if rate_limited else Signal.SEND_FAILED, detail=str(error)
        )

    async def connection_changed(self, state: str, *, reason: int | None = None) -> None:
        """Evolution's `connection.update`, as the maintainer needs to hear it.

        Called by whatever receives that webhook - the webhook leg, which is its
        own ticket - so until then this is reached from the tests only.

        A link that is down stops all sending and is worth waking somebody for,
        and if Baileys named a reason that reads as a ban it is different news
        again. A link that is *up* is not an alert, but it is not nothing either:
        section 4 of the playbook says a cold resume at full rate is how the 463
        reach-out limit gets tripped, so a reconnect puts her back on the
        week-one clamp. Anything else Baileys reports - `connecting`, most of all
        - changes neither.

        Section 5 of the deployment spec answers a plain disconnect with "pause
        all sending", and the mechanism is the ramp's halt rather than the soft
        pause. The switch is the *operator's*: flipping it here would need an
        automatic unflip, which is how a human's deliberate "cool it for a bit"
        gets silently undone by a reconnect. The ban shapes do flip it - those
        wait for a human by design.

        The heartbeat is untouched throughout, which is the distinction the whole
        alert exists to draw: the agent is alive, the number is not sending.
        """
        said = state.strip().casefold()
        if said in CONNECTED_STATES:
            await self._tell_the_ramp("resume after a reconnect", lambda ramp: ramp.link_up())
            return
        if said not in DISCONNECTED_STATES:
            return

        described = f", reason {reason}" if reason is not None else ""
        detail = f"Evolution reports the connection as {state!r}{described}."
        await self._tell_the_ramp("stop sending", lambda ramp: ramp.link_down(detail))
        signal = BAN_REASONS.get(reason, Signal.DISCONNECTED) if reason is not None else None
        await self.report(signal or Signal.DISCONNECTED, detail=detail)

    async def brain_failed(self, error: BaseException) -> None:
        """The brain's hook: DeepSeek gave no usable answer and the item was dropped."""
        await self.report(Signal.BRAIN_ERROR, detail=str(error))

    async def report(self, signal: Signal, *, detail: str = "") -> None:
        """Alert on one signal, stopping the sending first if the signal calls for it.

        In that order: a banned number that keeps sending is the harm this whole
        module exists to prevent, so the switch is flipped before anybody is told.
        """
        lines = [f"{signal}: {HEADLINES[signal]}"]
        if detail:
            lines.append(detail)
        if signal in STOPS_SENDING and await self._stop_sending(signal):
            lines.append(PAUSED_NOTE)
        try:
            await self._alerter.alert("\n".join(lines), key=f"signal:{signal}")
        except Exception as exc:
            # Both hooks above are called from inside an `except` block. An alerter
            # that threw would replace the failure the caller is handling with one
            # about the telling of it, which is the least useful exception there is.
            logger.error("could not raise the %s alert: %s", signal, exc)

    async def _stop_sending(self, signal: Signal) -> bool:
        """Flip the soft pause, and say whether it went. Never raises.

        A switch that could not be written - the `rebe` database being down is the
        realistic way - must not swallow the worst news the bot has. The alert
        goes out either way; it just does not claim she is paused when she is not.
        """
        if self._pause is None:
            return False
        try:
            await self._pause.set_paused(True, reason=f"{signal} seen by the ops channel")
        except Exception as exc:  # a broken switch is not a reason to lose the alert
            logger.error("could not pause after %s: %s", signal, exc)
            return False
        return True

    async def _tell_the_ramp(self, what: str, move: Callable[[Ramp], Awaitable[None]]) -> None:
        """Move the ramp, and never let that be why an alert did not go out.

        Same forgiveness as the switch above, and for the same reason: the `rebe`
        database being briefly unreachable must not swallow the news that the
        link is down, which is exactly the moment somebody needs to hear it.
        """
        if self._ramp is None:
            return
        try:
            await move(self._ramp)
        except Exception as exc:
            logger.error("could not %s: %s", what, exc)
