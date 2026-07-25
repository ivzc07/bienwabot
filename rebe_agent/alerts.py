"""How the maintainer hears about something out of band.

The real channel is Telegram, chosen in section 2.4 of the deployment spec
precisely because it does not depend on WhatsApp: an alert about Evolution being
down cannot travel through Evolution. That transport, its rate limiting, and the
soft-pause switch are ticket #23; this module is only the seam those plug into,
so the call-rate guard can raise an alert today without waiting for them.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger("rebe_agent.alerts")


class Alerter(Protocol):
    """Somewhere to send a message a human is expected to read."""

    async def alert(self, message: str) -> None: ...


class LoggingAlerter:
    """Writes the alert to the log at WARNING. The default until #23 lands."""

    async def alert(self, message: str) -> None:
        logger.warning("ALERT %s", message)
