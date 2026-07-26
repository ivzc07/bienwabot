"""The free half of the pipeline: what gets dropped, and what wins.

Every assertion here is about something that happens *before* a DeepSeek call is
spent, which is the point of the module - a stale item, a broken link or an HN
story nobody upvoted must never cost a token.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from rebe_agent.curate import Filters, Ranking, collapse, rank, score, shortlist, usable
from tests.support import NOON, item

FRESH = NOON - timedelta(hours=2)


def test_a_fresh_usable_item_survives() -> None:
    assert usable(item(published_at=FRESH), NOON)


def test_yesterdays_news_is_not_news() -> None:
    stale = item(published_at=NOON - timedelta(hours=40))

    assert not usable(stale, NOON)
    assert usable(stale, NOON, Filters(freshness=timedelta(hours=48)))


def test_an_item_dated_in_the_future_is_not_trusted_to_the_top() -> None:
    """Recency decay would otherwise park a mis-dated feed entry at rank one for
    as long as the feed carried it."""
    assert not usable(item(published_at=NOON + timedelta(days=2)), NOON)


@pytest.mark.parametrize("url", ["", "not-a-url", "mailto:rebe@bien.mx", "/relative"])
def test_an_item_with_no_resolvable_link_is_dropped(url: str) -> None:
    assert not usable(item(url=url), NOON)


@pytest.mark.parametrize("title", ["", "   ", "AI", "Nuevo"])
def test_an_item_with_no_usable_title_is_dropped(title: str) -> None:
    """A headline this short gives the model nothing to restate, and the framing
    line may not invent the rest."""
    assert not usable(item(title=title), NOON)


def test_hn_candidates_below_the_points_floor_are_dropped() -> None:
    assert not usable(item(source="hackernews", points=40), NOON)
    assert usable(item(source="hackernews", points=140), NOON)


def test_a_vendor_feed_is_not_judged_on_points_it_never_had() -> None:
    """`None` points means "this source has no popularity signal", which is not
    the same as "nobody upvoted it"."""
    assert usable(item(source="openai", points=None), NOON)


def test_a_first_party_launch_outranks_a_lesser_source_of_the_same_age() -> None:
    launch = item(source="openai", source_id="a", authority=1.0, title="OpenAI lanza algo")
    roundup = item(source="venturebeat", source_id="b", authority=0.4, title="Diez cosas de IA")

    assert score(launch, NOON) > score(roundup, NOON)


def test_a_busier_hn_story_outranks_a_quieter_one() -> None:
    busy = item(source="hackernews", source_id="1", points=800, comments=300, authority=0.7)
    quiet = item(source="hackernews", source_id="2", points=110, comments=4, authority=0.7)

    assert score(busy, NOON) > score(quiet, NOON)


def test_popularity_saturates_so_one_viral_item_cannot_own_the_day() -> None:
    big = item(source="hackernews", source_id="1", points=1200, authority=0.7)
    huge = item(source="hackernews", source_id="2", points=12000, authority=0.7)

    assert score(huge, NOON) - score(big, NOON) < 0.02


def test_the_fresher_of_two_equals_wins() -> None:
    older = item(source_id="a", published_at=NOON - timedelta(hours=20))
    newer = item(source_id="b", published_at=NOON - timedelta(hours=1))

    assert rank([older, newer], NOON) == [newer, older]


def test_ranking_is_repeatable_when_two_items_score_the_same() -> None:
    left = item(source="a", source_id="1", title="Un modelo nuevo sale hoy", url="https://a.mx/1")
    right = item(source="b", source_id="2", title="Otro modelo sale hoy ya", url="https://b.mx/2")

    assert rank([left, right], NOON) == rank([right, left], NOON)


def test_the_same_article_from_two_sources_collapses_to_one() -> None:
    """The acceptance criterion, on the layer it names: HN and the vendor's own
    feed carry the same launch, and the group sees it once."""
    from_hn = item(
        source="hackernews",
        source_id="41234567",
        title="OpenAI ships a local model",
        url="https://openai.com/index/local?utm_source=hn",
        points=600,
        authority=0.7,
    )
    from_vendor = item(
        source="openai",
        source_id="guid-1",
        title="Un titulo completamente distinto",
        url="https://www.openai.com/index/local/",
        authority=1.0,
    )

    survivors = collapse([from_hn, from_vendor])

    assert survivors == [from_hn]


def test_a_repost_under_a_different_url_collapses_on_the_title() -> None:
    original = item(source="openai", source_id="1", url="https://openai.com/index/x")
    syndicated = item(source="techcrunch", source_id="2", url="https://techcrunch.com/2026/x")

    assert collapse([original, syndicated]) == [original]


def test_collapsing_keeps_the_better_ranked_of_a_duplicate_pair() -> None:
    """Ranking before collapsing is what makes this true; the other order would
    keep whichever source happened to be fetched first."""
    weak = item(
        source="venturebeat",
        source_id="b",
        authority=0.3,
        url="https://openai.com/index/x?utm_source=vb",
        published_at=NOON - timedelta(hours=10),
    )
    strong = item(
        source="openai",
        source_id="a",
        authority=1.0,
        url="https://openai.com/index/x",
        published_at=FRESH,
    )

    assert shortlist([weak, strong], NOON) == [strong]


def test_the_shortlist_drops_then_ranks_then_collapses() -> None:
    stale = item(source_id="stale", published_at=NOON - timedelta(days=3), url="https://a.mx/1")
    broken = item(source_id="broken", url="not-a-url")
    cheap = item(source="hackernews", source_id="cheap", points=3, url="https://a.mx/2")
    good = item(source_id="good", url="https://a.mx/3", published_at=FRESH)

    assert list(shortlist([stale, broken, cheap, good], NOON)) == [good]


def test_the_weights_are_a_parameter_not_a_constant() -> None:
    """A weight that turns out wrong is tuned here, not rewritten."""
    old_but_authoritative = item(authority=1.0, published_at=NOON - timedelta(hours=30))
    new_but_minor = item(source_id="b", authority=0.2, published_at=NOON)
    recency_first = Ranking(authority=0.1, popularity=0.1, recency=0.8)

    assert rank([old_but_authoritative, new_but_minor], NOON)[0] is old_but_authoritative
    assert rank([old_but_authoritative, new_but_minor], NOON, recency_first)[0] is new_but_minor
