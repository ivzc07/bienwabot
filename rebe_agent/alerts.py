"""How the maintainer hears about something out of band, and what they hear.

The channel is Telegram, chosen in section 2.4 of the deployment spec precisely
because it does not depend on WhatsApp: an alert about Evolution being down
cannot travel through Evolution.

Three ideas live here.

**A seam.** `Alerter` is "somewhere to send a message a human is expected to
read". The call-rate guard and the pacer talk to that, never to Telegram, so a
test can read what would have been sent and no test ever reaches
api.telegram.org.

**A rate limit.** Section 5 of the deployment spec answers a 463 and a temporary
ban with "back off and alert", and a signal that repeats forty times must not
become forty messages - an alert storm is the same as no alerts. So identical
alerts are collapsed into one per window, and the repeats that were held back are
counted into the next one rather than lost.

**A vocabulary.** `Signal` is the closed set of things worth waking somebody for,
from section 4 of the anti-ban playbook and section 5 of the deployment spec. Each
one carries the sentence a maintainer needs at 2 a.m.: what happened, whether Rebe
is still sending, and what to do. "Alert" and "act on it" are the same message or
the alert is noise.

Two of those signals - the ban shapes - also *stop* Rebe, by flipping the same
soft-pause switch an operator uses, because "stop sending, do not keep hammering"
needs a mechanism and that is the one there is. Coming back is a human decision:
the temporary shape wants waiting out, the permanent one wants the backup number,
and getting those two the wrong way round either burns the only warm standby or
leaves Rebe dead for good.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from rebe_agent.clock import Clock
from rebe_agent.evolution import EvolutionError, EvolutionRateLimitedError
from rebe_agent.pause import PauseSwitch
from rebe_agent.telegram import TelegramClient, TelegramError

logger = logging.getLogger("rebe_agent.alerts")

ALERT_WINDOW = timedelta(minutes=30)
"""How long one signal stays quiet after it has been reported once."""


class Alerter(Protocol):
    """Somewhere to send a message a human is expected to read."""

    async def alert(self, message: str, *, key: str | None = None) -> None:
        """Tell the maintainer. Never raises: an undeliverable alert is not the
        caller's problem, and failing the caller would hide the original failure.

        `key` is what "the same alert again" means for rate limiting. It defaults
        to the message, which is right for a one-off line and wrong for a signal
        whose detail text moves - two 463s worded differently are one signal.
        """


class LoggingAlerter:
    """Writes the alert to the log at WARNING. The default when no channel is wired."""

    async def alert(self, message: str, *, key: str | None = None) -> None:
        logger.warning("ALERT %s", message)


class TelegramAlerter:
    """Puts the alert in the ops chat, out of band from WhatsApp."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def alert(self, message: str, *, key: str | None = None) -> None:
        try:
            await self._client.send_message(message)
        except TelegramError as exc:
            # Nowhere left to escalate to: the log is the last channel there is.
            logger.error("could not deliver an alert (%s): %s", exc, message)


@dataclass(frozen=True, slots=True)
class _Reported:
    """When a key was last alerted on, and how many repeats came after it."""

    at: datetime
    held: int = 0


class ThrottledAlerter:
    """One alert per key per window. Repeats are counted, not sent."""

    def __init__(self, inner: Alerter, clock: Clock, *, window: timedelta = ALERT_WINDOW) -> None:
        self._inner = inner
        self._clock = clock
        self._window = window
        self._reported: dict[str, _Reported] = {}

    async def alert(self, message: str, *, key: str | None = None) -> None:
        identity = key or message
        now = self._clock.now()
        last = self._reported.get(identity)

        if last is not None and now - last.at < self._window:
            self._reported[identity] = _Reported(at=last.at, held=last.held + 1)
            logger.info("holding a repeat of %r (%d so far)", identity, last.held + 1)
            return

        held = last.held if last is not None else 0
        self._reported[identity] = _Reported(at=now)
        await self._inner.alert(_with_repeats(message, held, self._window), key=identity)


def _with_repeats(message: str, held: int, window: timedelta) -> str:
    """The alert, plus what the last window swallowed. Silence is not the same as none."""
    if held == 0:
        return message
    minutes = int(window.total_seconds() // 60)
    return f"{message}\n({held} more like this in the last {minutes} minutes.)"


class Signal(StrEnum):
    """What is worth telling a human about, out of band. A closed set on purpose.

    The value is what appears in the alert, so these strings are read by people.
    """

    RATE_LIMITED = "rate_limited"
    """A 463 reach-out time-lock or a 429 on send: WhatsApp is pushing back."""

    SEND_FAILED = "send_failed"
    """Evolution would not take a message, for a reason that is not a rate limit."""

    DISCONNECTED = "disconnected"
    """Evolution reports the WhatsApp link as down. Nothing can go out."""

    TEMPORARY_BAN = "temporary_ban"
    """The session was ended by WhatsApp. Wait it out; never swap the number."""

    PERMANENT_BAN = "permanent_ban"
    """WhatsApp refuses the number. The backup instance is the only way back."""

    BRAIN_ERROR = "brain_error"
    """A DeepSeek call failed. The group sees nothing, which is the design."""


HEADLINES: Mapping[Signal, str] = {
    Signal.RATE_LIMITED: (
        "WhatsApp is rate-limiting Rebe's sends - a 463 reach-out time-lock or a 429. "
        "Nothing is being retried. If it keeps up, pause her for a few hours with /pausa."
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
        "on the phone, wait it out, then resume with /reanuda. Do not swap to the "
        "backup number for this."
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
"""One sentence of diagnosis and one of instruction, per signal."""

STOPS_SENDING = frozenset({Signal.TEMPORARY_BAN, Signal.PERMANENT_BAN})
"""The signals that also silence Rebe until a human decides otherwise."""

PAUSED_NOTE = "Rebe is now paused and will send nothing until you resume her with /reanuda."

DISCONNECTED_STATES = frozenset({"close", "closed", "disconnected"})
"""Evolution's `connection.update` states that mean the link is not usable."""

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


class Watchtower:
    """Turns what the agent observed into one out-of-band alert a human can act on.

    Deliberately not a classifier over free text: every entry point takes a typed
    signal or a typed error, because "is this a ban?" is a decision worth reading
    in one place rather than guessing at from a log line.
    """

    def __init__(self, alerter: Alerter | None = None, *, pause: PauseSwitch | None = None) -> None:
        self._alerter = alerter or LoggingAlerter()
        self._pause = pause

    async def send_failed(self, error: EvolutionError) -> None:
        """The pacer's hook: a message did not get out.

        A 463 or a 429 is the ban-adjacent signal the playbook cares about; every
        other transport failure is still worth one throttled line, because a bot
        that cannot send and says nothing looks exactly like a bot with nothing
        to say.
        """
        rate_limited = isinstance(error, EvolutionRateLimitedError)
        await self.report(
            Signal.RATE_LIMITED if rate_limited else Signal.SEND_FAILED, detail=str(error)
        )

    async def connection_changed(self, state: str, *, reason: int | None = None) -> None:
        """Evolution's `connection.update`, as the maintainer needs to hear it.

        A link that is up is not news. A link that is down is, and if Baileys
        named a reason that reads as a ban, it is different news again.
        """
        if state.strip().casefold() not in DISCONNECTED_STATES:
            return
        signal = BAN_REASONS.get(reason, Signal.DISCONNECTED) if reason is not None else None
        described = f", reason {reason}" if reason is not None else ""
        await self.report(
            signal or Signal.DISCONNECTED,
            detail=f"Evolution reports the connection as {state!r}{described}.",
        )

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
        await self._alerter.alert("\n".join(lines), key=f"signal:{signal}")

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
