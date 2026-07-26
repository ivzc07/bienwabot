"""The heartbeat: a push to Uptime Kuma, from inside the loop that does the work.

Section 2.4 of `docs/wayfinder/deployment-architecture-spec.md` asks for a beat
about every sixty seconds, and a missed beat is what makes Kuma fire the Telegram
alert. Two decisions in that sentence are worth restating.

**Push, not pull.** Kuma calls nothing; the agent calls Kuma. So the agent needs
no exposed health port, which matters because section 2.2 gives it no public URL
at all.

**From inside the loop.** A `GET /health` handler answers for as long as the
process is alive, which means a bot whose loop has wedged - a deadlock, a
synchronous call that never returns - reports itself healthy while the group hears
nothing. This beat is a coroutine on the same event loop as both legs, so anything
that stops the loop turning also stops the beat, and Kuma turns that into an alert
within a minute. That is the difference between detecting a crash and detecting a
hang.

A failed push is a warning and nothing more. Kuma being unreachable is not an
outage, and a heartbeat that raised would take the process down - turning the
monitoring into the thing it was watching for. The missed beat is already the
alarm, so there is nothing to add by failing.

Nothing here knows about the soft pause: a paused Rebe is a working Rebe that has
been asked to be quiet, and the beat is exactly how an operator tells a pause
apart from an outage.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx

from rebe_agent.config import Settings

logger = logging.getLogger("rebe_agent.heartbeat")

HEARTBEAT_SECONDS = 60.0
"""How often the beat goes out. The Kuma monitor's own timeout is set against this."""

PUSH_TIMEOUT_SECONDS = 10.0
"""Kuma is one hop away on the internal network. A slow push is a lost push."""

UP = "up"
"""Kuma's own word for a monitor that is fine."""

ALIVE = "rebe-agent loop alive"
"""What shows up next to the beat in Kuma's history."""


class Heartbeat:
    """One push per interval, for as long as the loop it lives on keeps turning."""

    def __init__(
        self,
        push_url: str,
        http_client: httpx.AsyncClient,
        *,
        interval: float = HEARTBEAT_SECONDS,
        timeout: float = PUSH_TIMEOUT_SECONDS,
    ) -> None:
        self._push_url = push_url
        self._http = http_client
        self._timeout = timeout
        self.interval = interval
        """Seconds between beats. Read by tests, and by nobody in production."""

    async def beat(self, message: str = ALIVE) -> None:
        """Push once. Never raises, whatever Kuma or the network does.

        The push URL carries a monitor token, so nothing here logs the URL: a
        warning that leaked it would let anybody keep the monitor green.
        """
        try:
            response = await self._http.get(
                self._push_url,
                params={"status": UP, "msg": message},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("the Kuma heartbeat did not go out: %s", type(exc).__name__)
            return
        if response.status_code >= 400:
            logger.warning("Kuma refused the heartbeat with HTTP %d", response.status_code)

    async def run(self, stopping: asyncio.Event) -> None:
        """Beat until `stopping` is set, then return.

        The wait is on the stop signal rather than on a plain sleep, so a SIGTERM
        does not have to sit out the rest of an interval before the container can
        exit.
        """
        logger.info("heartbeat every %.0fs", self.interval)
        while not stopping.is_set():
            await self.beat()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), self.interval)
        logger.info("heartbeat stopped")


def build_heartbeat(settings: Settings, http_client: httpx.AsyncClient) -> Heartbeat:
    """The heartbeat for whichever monitor `KUMA_PUSH_URL` names."""
    return Heartbeat(settings.kuma_push_url, http_client)
