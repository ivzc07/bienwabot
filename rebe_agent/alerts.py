"""How the maintainer hears about something out of band. Not what they hear.

The channel is Telegram, chosen in section 2.4 of the deployment spec precisely
because it does not depend on WhatsApp: an alert about Evolution being down
cannot travel through Evolution. What is worth telling somebody, and in what
words, is `rebe_agent.signals`; this module is only the way out.

Two ideas live here.

**A seam.** `Alerter` is "somewhere to send a message a human is expected to
read". The call-rate guard and the watchtower talk to that, never to Telegram, so
a test can read what would have been sent and no test ever reaches
api.telegram.org.

**A rate limit.** Section 5 of the deployment spec answers a 463 and a temporary
ban with "back off and alert", and a signal that repeats forty times must not
become forty messages - an alert storm is the same as no alerts. So identical
alerts are collapsed into one per window, and the repeats that were held back are
counted into the next one rather than lost.

Nothing here raises. An alert is always *about* a failure, and an alerter that
threw would hand the caller a second failure about the telling of the first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from rebe_agent.clock import Clock
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
    """One alert per key per window. Repeats are counted, not sent.

    The window lives in memory rather than in the `rebe` database, unlike the
    soft pause next door. That is deliberate: this is the path that has to work
    *while* something is broken, and the realistic broken thing is the database.
    A restart therefore re-opens every window - a crash loop can alert once per
    boot - which is the right way to be wrong, because a crash loop is itself
    news and Kuma is already saying so.
    """

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
