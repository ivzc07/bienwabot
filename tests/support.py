"""Small pieces more than one test module needs."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from rebe_agent.items import NewsItem

FIXTURES = Path(__file__).parent / "fixtures"
"""Recorded responses. Nothing in the test run touches the network."""


def fixture(name: str) -> bytes:
    """One recorded payload, as the bytes an HTTP response would carry."""
    return (FIXTURES / name).read_bytes()


MEXICO_CITY = ZoneInfo("America/Mexico_City")

NOON = datetime(2026, 7, 25, 12, 0, tzinfo=MEXICO_CITY)
"""Somewhere in the middle of a day, so a test never trips over a date boundary."""

TODAY = NOON.date()

GROUP = "120363000000000000@g.us"
"""The bien.mx group, as far as any test is concerned."""


def item(
    *,
    source: str = "openai",
    source_id: str = "item-1",
    title: str = "OpenAI lanza un modelo que corre local",
    url: str = "https://openai.com/index/nuevo-modelo",
    published_at: datetime | None = None,
    authority: float = 1.0,
    points: int | None = None,
    comments: int | None = None,
    summary: str = "",
) -> NewsItem:
    """A candidate with everything filled in, so a test names only what it is about."""
    return NewsItem(
        source=source,
        source_id=source_id,
        title=title,
        url=url,
        published_at=published_at if published_at is not None else NOON - timedelta(hours=1),
        authority=authority,
        points=points,
        comments=comments,
        summary=summary,
    )


class RecordingAlerter:
    """Keeps what would have gone to the maintainer, so a test can read it."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.keys: list[str] = []

    async def alert(self, message: str, *, key: str | None = None) -> None:
        self.messages.append(message)
        self.keys.append(key or message)
