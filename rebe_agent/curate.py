"""Turning a raw pool of candidates into the one item worth a DeepSeek call.

Section 2 of `docs/wayfinder/news-pipeline-research.md` puts filtering and ranking
ahead of breadth, because the audience is a general Mexican group and not a
research feed. Everything here is deterministic and free: it runs before a single
token is spent, so a stale item, a headline with nothing in it, a broken link or
an HN story nobody cared about never reaches the model.

The order matters and is not arbitrary.

1. **Filter.** Freshness, a usable title, a resolvable URL, and the HN points
   floor. Cheapest first, and every one of them is a reason the item could never
   become a good post.
2. **Rank.** Source authority, popularity, recency decay, combined into one
   transparent number. Transparent on purpose: when a bad item gets posted, the
   score has to be readable by a human deciding which weight was wrong.
3. **Collapse.** Only then are duplicates removed, so the survivor of a duplicate
   pair is the *better-ranked* of the two rather than whichever source happened
   to be fetched first. The same launch from HN and from the vendor's own feed
   becomes one entry, and it is the entry with the stronger signal.

Dropping the already-posted ones is not here: that needs the store, so it belongs
to the leg in `rebe_agent.news`. What is here needs nothing but the candidates
and the time.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from rebe_agent.items import NewsItem, SeenItems

logger = logging.getLogger("rebe_agent.curate")


@dataclass(frozen=True, slots=True)
class Filters:
    """The free quality gates, all of them applied before any model call."""

    freshness: timedelta = timedelta(hours=36)
    """How old an item may be. The group should never be handed yesterday's news
    as if it were new, and 36 hours covers a run that missed a day."""

    points_floor: int = 100
    """HN's own free quality signal. Applied to items that carry a points count;
    a first-party vendor feed has no points and is not judged by this."""

    minimum_title_chars: int = 12
    """Below this a headline gives the model nothing to restate, and the framing
    line would have to invent the story - which is exactly what it may not do."""


@dataclass(frozen=True, slots=True)
class Ranking:
    """How the three signals are traded off against each other.

    Authority leads because the brief is launch news for a general audience:
    "OpenAI shipped a thing" is interesting to this group in a way that a
    well-upvoted engineering post is not.
    """

    authority: float = 0.45
    popularity: float = 0.30
    recency: float = 0.25

    half_life: timedelta = timedelta(hours=12)
    """After this long an item's recency term is worth half. Fast, because a
    day-old AI launch is already background noise."""

    points_ceiling: int = 1000
    comments_ceiling: int = 500
    """Where the popularity terms saturate. A 4,000-point story is not four times
    more interesting than a 1,000-point one, and a linear term would let one
    viral item outrank every launch for a day."""

    comments_share: float = 0.3
    """How much of the popularity term is discussion rather than upvotes."""


DEFAULT_FILTERS = Filters()
DEFAULT_RANKING = Ranking()
"""The shipped posture. Named, so a caller can pass its own without rebuilding
the object on every candidate."""


def usable(item: NewsItem, now: datetime, filters: Filters = DEFAULT_FILTERS) -> bool:
    """Could this item ever become a good post? Answered without spending anything."""
    if not item.canonical_url:
        return False
    if len(item.title.strip()) < filters.minimum_title_chars:
        return False
    if item.published_at > now + timedelta(minutes=5):
        # A clock skew of a few minutes is normal; a feed dated next week is not,
        # and it would sit at the top of the recency term forever.
        return False
    if now - item.published_at > filters.freshness:
        return False
    return item.points is None or item.points >= filters.points_floor


def score(item: NewsItem, now: datetime, ranking: Ranking = DEFAULT_RANKING) -> float:
    """One number in roughly `[0, 1]`, readable term by term."""
    return (
        ranking.authority * _clamp(item.authority)
        + ranking.popularity * _popularity(item, ranking)
        + ranking.recency * _recency(item, now, ranking)
    )


def _popularity(item: NewsItem, ranking: Ranking) -> float:
    """How much the internet cared, saturating so one viral item cannot dominate.

    A source with no popularity signal at all scores zero here rather than being
    guessed at; it earns its place on authority instead.
    """
    if item.points is None and item.comments is None:
        return 0.0
    upvotes = _saturating(item.points or 0, ranking.points_ceiling)
    discussion = _saturating(item.comments or 0, ranking.comments_ceiling)
    return (1 - ranking.comments_share) * upvotes + ranking.comments_share * discussion


def _recency(item: NewsItem, now: datetime, ranking: Ranking) -> float:
    """Exponential decay on the item's own timestamp, halving each half-life."""
    age = (now - item.published_at).total_seconds()
    if age <= 0:
        return 1.0
    return float(0.5 ** (age / ranking.half_life.total_seconds()))


def _saturating(value: int, ceiling: int) -> float:
    """A logarithmic `[0, 1]` term: differences matter most while numbers are small."""
    if value <= 0 or ceiling <= 0:
        return 0.0
    return min(math.log1p(value) / math.log1p(ceiling), 1.0)


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def rank(
    items: Iterable[NewsItem], now: datetime, ranking: Ranking = DEFAULT_RANKING
) -> list[NewsItem]:
    """Best first. Ties break on recency, then on the source key, so a run is
    repeatable and two candidates that score identically do not swap places
    between runs for no reason."""
    return sorted(
        items,
        key=lambda item: (
            -score(item, now, ranking),
            -item.published_at.timestamp(),
            item.source_key,
        ),
    )


def collapse(items: Iterable[NewsItem]) -> list[NewsItem]:
    """Drop every candidate that is already represented, keeping the first.

    Fed a ranked list, "the first" is "the best", which is what makes this the
    right way round: the HN entry with 400 points and the vendor's own post about
    the same launch become one item, and which one survives is decided by the
    score rather than by fetch order.
    """
    kept: list[NewsItem] = []
    seen = SeenItems()
    for item in items:
        if seen.knows(item):
            logger.debug("collapsing a duplicate of %s from %s", item.canonical_url, item.source)
            continue
        seen.remember(item)
        kept.append(item)
    return kept


def shortlist(
    items: Iterable[NewsItem],
    now: datetime,
    *,
    filters: Filters = DEFAULT_FILTERS,
    ranking: Ranking = DEFAULT_RANKING,
) -> Sequence[NewsItem]:
    """Filter, rank, collapse: the whole free half of the pipeline, in order."""
    survivors = [item for item in items if usable(item, now, filters)]
    return collapse(rank(survivors, now, ranking))
