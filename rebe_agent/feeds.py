"""Where the candidates come from: Hacker News, and the AI orgs' own feeds.

Section 1 of `docs/wayfinder/news-pipeline-research.md` picks both layers and
says why.

**Hacker News through the keyless Algolia search API.** One request returns
fully-hydrated stories already filtered by a points floor, where the Firebase API
would need one call per story. Points are a free quality signal that costs
nothing to apply, and HN is the best real-time read on what the tech world found
notable. The freshness window is pushed into the query too, so the response is
already the shape the curator wants.

**The verified first-party RSS feeds.** Vendor blogs are where launches are
*announced*, which is the news a general Mexican audience actually cares about -
"a new model shipped", not "a paper was posted". Only the feeds the research
fetched first-party are here. Anthropic, Meta AI and Google Research are absent
because they have no first-party feed; The Verge and Ars Technica are absent
because their URLs were confirmed from a directory rather than fetched, and a
feed nobody has read is a guess.

The research suggested `feedparser` for this layer. The stdlib XML parser is used
instead: eight known feeds and four fields each is a small enough surface that the
dependency buys mostly leniency towards malformed XML, and a feed that fails to
parse is already handled the same way as a feed that fails to load - logged, and
the run continues on the others.

Nothing here filters, ranks or deduplicates. It answers "what is out there", and
`rebe_agent.curate` answers "which of it is worth a call".
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Protocol
from xml.etree import ElementTree

import httpx

from rebe_agent.items import NewsItem

logger = logging.getLogger("rebe_agent.feeds")

USER_AGENT = "rebe-agent/0.1 (+https://bien.mx)"
"""Several of these hosts refuse a client that does not name itself."""

REQUEST_TIMEOUT_SECONDS = 15.0
"""A source that is slow is a source this run does without."""

MAX_SUMMARY_CHARS = 400
"""Enough for the model to say what happened; not enough to become the article."""

HN_ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"
HN_SOURCE = "hackernews"
HN_AUTHORITY = 0.7
HN_QUERY = "AI"
HN_HITS = 50
"""One page is plenty: the window is a day and a half and the floor is high."""


@dataclass(frozen=True, slots=True)
class Feed:
    """One RSS or Atom feed, and how much its word is worth.

    The weight travels with the source so that adding a feed is a one-line change
    here rather than an edit in the ranker as well.
    """

    key: str
    """Stable, and stored: it is half of the item's source ID in the posted store."""

    name: str
    url: str
    authority: float


FEEDS: tuple[Feed, ...] = (
    Feed("openai", "OpenAI", "https://openai.com/news/rss.xml", 1.0),
    Feed("deepmind", "Google DeepMind", "https://deepmind.google/blog/rss.xml", 1.0),
    Feed("googleai", "Google AI", "https://blog.google/technology/ai/rss/", 0.9),
    Feed("huggingface", "Hugging Face", "https://huggingface.co/blog/feed.xml", 0.85),
    Feed(
        "microsoft-research",
        "Microsoft Research",
        "https://www.microsoft.com/en-us/research/feed/",
        0.8,
    ),
    Feed(
        "technologyreview",
        "MIT Technology Review",
        "https://www.technologyreview.com/topic/artificial-intelligence/feed/",
        0.6,
    ),
    Feed(
        "techcrunch",
        "TechCrunch",
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        0.5,
    ),
    Feed("venturebeat", "VentureBeat", "https://venturebeat.com/category/ai/feed/", 0.45),
)
"""First-party launch announcements first, general tech press last."""


class Candidates(Protocol):
    """Whatever can hand the news leg a pool of items to choose from."""

    async def fetch(self, now: datetime) -> Sequence[NewsItem]:
        """Everything on offer right now, unfiltered and unranked.

        A source that fails is a source this run does without: the return is what
        could be reached, never an exception, because one dead feed must not cost
        the group its post.
        """


class WebCandidates:
    """The real pool: one HN query and the vendor feeds, fetched together."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        feeds: Iterable[Feed] = FEEDS,
        points_floor: int = 100,
        freshness: timedelta = timedelta(hours=36),
        query: str = HN_QUERY,
    ) -> None:
        self._http = http
        self._feeds = tuple(feeds)
        self._points_floor = points_floor
        self._freshness = freshness
        self._query = query

    async def fetch(self, now: datetime) -> Sequence[NewsItem]:
        sources = [self._hacker_news(now), *(self._feed(feed) for feed in self._feeds)]
        gathered = await asyncio.gather(*sources, return_exceptions=True)

        items: list[NewsItem] = []
        named = zip((HN_SOURCE, *(feed.key for feed in self._feeds)), gathered, strict=True)
        for source, result in named:
            if isinstance(result, BaseException):
                logger.warning("%s did not answer, skipping it this run: %s", source, result)
                continue
            items.extend(result)
        logger.info("%d candidates from %d sources", len(items), len(sources))
        return items

    async def _hacker_news(self, now: datetime) -> list[NewsItem]:
        """One request: recent stories, above the points floor, already hydrated."""
        since = int((now - self._freshness).timestamp())
        response = await self._http.get(
            HN_ENDPOINT,
            params={
                "query": self._query,
                "tags": "story",
                "numericFilters": f"points>={self._points_floor},created_at_i>={since}",
                "hitsPerPage": HN_HITS,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return hacker_news_items(response.json())

    async def _feed(self, feed: Feed) -> list[NewsItem]:
        response = await self._http.get(
            feed.url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()
        return feed_items(feed, response.content)


def hacker_news_items(payload: object) -> list[NewsItem]:
    """Turn one Algolia search response into candidates.

    A hit with no `url` is an Ask HN or a text post, which has no article to
    link; it is left with an empty URL and dropped by the usable-item filter
    rather than being special-cased twice.
    """
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        logger.warning("the HN search answered with no hits list")
        return []

    items: list[NewsItem] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        created = _hn_moment(hit)
        title = _string(hit.get("title"))
        if created is None or not title:
            continue
        items.append(
            NewsItem(
                source=HN_SOURCE,
                source_id=_string(hit.get("objectID")),
                title=title,
                url=_string(hit.get("url")),
                published_at=created,
                authority=HN_AUTHORITY,
                points=_count(hit.get("points")),
                comments=_count(hit.get("num_comments")),
                summary=_plain_text(_string(hit.get("story_text"))),
            )
        )
    return items


def _hn_moment(hit: dict[str, Any]) -> datetime | None:
    """`created_at_i` is the documented epoch field; `created_at` is the fallback."""
    epoch = hit.get("created_at_i")
    if isinstance(epoch, int | float):
        return datetime.fromtimestamp(float(epoch), tz=UTC)
    return _moment(_string(hit.get("created_at")))


ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"
DUBLIN_DATE = "{http://purl.org/dc/elements/1.1/}date"


def feed_items(feed: Feed, body: bytes) -> list[NewsItem]:
    """Turn one RSS or Atom document into candidates.

    Both dialects are read by the same walk, because the difference between them
    is which element name carries each of the four fields we want, and nothing
    else about this pipeline depends on knowing which one arrived.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        logger.warning("%s served XML that will not parse: %s", feed.key, exc)
        return []

    entries = root.findall(f"{ATOM}entry") or root.findall("channel/item")
    items: list[NewsItem] = []
    for entry in entries:
        title = _plain_text(_first(entry, f"{ATOM}title", "title"))
        url = _link(entry)
        published = _moment(
            _first(entry, f"{ATOM}published", f"{ATOM}updated", "pubDate", DUBLIN_DATE)
        )
        if not title or published is None:
            continue
        items.append(
            NewsItem(
                source=feed.key,
                source_id=_first(entry, f"{ATOM}id", "guid") or url,
                title=title,
                url=url,
                published_at=published,
                authority=feed.authority,
                summary=_plain_text(
                    _first(
                        entry,
                        f"{ATOM}summary",
                        "description",
                        f"{ATOM}content",
                        CONTENT_ENCODED,
                    )
                ),
            )
        )
    return items


def _link(entry: ElementTree.Element) -> str:
    """The article's own address, in whichever way the dialect says it.

    Atom puts it in an attribute and may carry several - a `self`, a `replies`,
    an `enclosure` - so the alternate one is chosen rather than the first one.
    """
    for candidate in entry.findall(f"{ATOM}link"):
        if candidate.get("rel", "alternate") == "alternate" and candidate.get("href"):
            return str(candidate.get("href", "")).strip()
    return _first(entry, "link")


def _first(entry: ElementTree.Element, *names: str) -> str:
    """The text of the first of `names` that is present and non-empty."""
    for name in names:
        found = entry.find(name)
        if found is not None and found.text and found.text.strip():
            return found.text.strip()
    return ""


_MARKUP = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?%)\]])")


def _plain_text(raw: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Feed summaries are HTML. The model is handed prose, not markup.

    Tags become a space rather than nothing, so `<p>one</p><p>two</p>` does not
    read as `onetwo`; the punctuation pass then closes the gap that leaves behind
    when the tag sat against a comma. The result is what the framing line is
    checked against, so a stray space is not cosmetic - it is part of the text
    the anti-hallucination rule reads.
    """
    if not raw:
        return ""
    spaced = _WHITESPACE.sub(" ", unescape(_MARKUP.sub(" ", raw)))
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", spaced).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _moment(raw: str) -> datetime | None:
    """A feed date, in either of the two formats feeds actually use.

    RSS carries RFC 822 (`Tue, 21 Jul 2026 09:00:00 GMT`) and Atom carries RFC
    3339 (`2026-07-21T09:00:00Z`). A date with no zone is read as UTC, because a
    naive timestamp compared against an aware one is a crash, and being an hour
    wrong about a freshness window is not.
    """
    if not raw:
        return None
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.debug("unreadable date %r", raw)
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _count(value: object) -> int | None:
    """A popularity number, or `None` when the source did not report one."""
    return int(value) if isinstance(value, int | float) else None
