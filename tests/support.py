"""Small pieces more than one test module needs."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

MEXICO_CITY = ZoneInfo("America/Mexico_City")

NOON = datetime(2026, 7, 25, 12, 0, tzinfo=MEXICO_CITY)
"""Somewhere in the middle of a day, so a test never trips over a date boundary."""

TODAY = NOON.date()

GROUP = "120363000000000000@g.us"
"""The bien.mx group, as far as any test is concerned."""


class RecordingAlerter:
    """Keeps what would have gone to the maintainer, so a test can read it."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def alert(self, message: str) -> None:
        self.messages.append(message)
