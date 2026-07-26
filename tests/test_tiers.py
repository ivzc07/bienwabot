"""The two tiers, read off the same recorded payloads the feeds layer parses.

Section 4 of the cadence spec says an item is high tier when it is top-of-HN by
points, well above the ranker's floor, or a first-party launch announcement from
a major AI org - and that commentary about a launch is not a launch. Every case
here is built from a fixture rather than from a hand-written object, so the thing
being classified is the shape the real sources actually return, carrying the
authority the shipped feed list gives it.
"""

from __future__ import annotations

import json

import pytest

from rebe_agent.curate import Filters, score
from rebe_agent.feeds import FEEDS, Feed, feed_items, hacker_news_items
from rebe_agent.items import NewsItem
from rebe_agent.tiers import Tier, TierBar, announces_a_launch, classify, weak
from tests.support import NOON, fixture, item


def feed_named(key: str) -> Feed:
    """One of the shipped feeds, so a fixture item carries its real authority."""
    return next(feed for feed in FEEDS if feed.key == key)


def items_from(name: str, key: str) -> list[NewsItem]:
    return feed_items(feed_named(key), fixture(name))


def hn_from(name: str) -> list[NewsItem]:
    return hacker_news_items(json.loads(fixture(name)))


def only(items: list[NewsItem], source_id: str) -> NewsItem:
    found = [candidate for candidate in items if candidate.source_id == source_id]
    assert len(found) == 1, f"expected exactly one {source_id}, got {len(found)}"
    return found[0]


# --- top of Hacker News ------------------------------------------------------


def test_a_top_of_hn_story_is_high_tier() -> None:
    """1,420 points is not "above the floor", it is the story of the day."""
    top = only(hn_from("hn_top_story.json"), "42000001")

    assert top.points == 1420
    assert classify(top) is Tier.HIGH


def test_an_ordinary_story_above_the_floor_is_not() -> None:
    """The floor is 100, and clearing it only means the item is worth ranking."""
    ordinary = only(hn_from("hn_top_story.json"), "42000002")

    assert ordinary.points == 150
    assert classify(ordinary) is Tier.NORMAL


def test_the_points_bar_is_a_multiple_of_the_rankers_own_floor() -> None:
    """Written as "well above the floor" rather than as a second constant, so a
    floor that moves takes the high-tier bar with it instead of quietly leaving
    every ordinary item high tier."""
    ordinary = only(hn_from("hn_top_story.json"), "42000002")

    assert classify(ordinary, filters=Filters(points_floor=25)) is Tier.HIGH


def test_a_well_upvoted_hn_launch_from_the_shipped_fixture_is_high_tier() -> None:
    launch = only(hn_from("hn_algolia.json"), "41000001")

    assert launch.points == 640
    assert classify(launch) is Tier.HIGH


# --- a first-party launch ----------------------------------------------------


def test_a_first_party_launch_is_high_tier_with_no_points_at_all() -> None:
    """A vendor blog has no popularity signal, and a launch there is exactly the
    news the group cares about: "a new model shipped"."""
    launch = only(items_from("openai_atom.xml", "openai"), "https://openai.com/index/local-model")

    assert launch.points is None
    assert announces_a_launch(launch)
    assert classify(launch) is Tier.HIGH


def test_introducing_something_is_an_announcement_too() -> None:
    launch = only(
        items_from("deepmind_atom.xml", "deepmind"),
        "https://deepmind.google/discover/blog/gemini-nano-3",
    )

    assert classify(launch) is Tier.HIGH


def test_commentary_from_the_same_major_org_is_not_a_launch() -> None:
    """The veto that matters: the source is first party and the summary even says
    "launch", but the headline is a reflection on one, not an announcement."""
    reflection = only(
        items_from("deepmind_atom.xml", "deepmind"),
        "https://deepmind.google/discover/blog/small-models-agents",
    )

    assert "launch" in reflection.summary
    assert not announces_a_launch(reflection)
    assert classify(reflection) is Tier.NORMAL


def test_the_press_writing_about_a_launch_is_not_a_launch() -> None:
    """Same headline as the first-party post, from a source that did not ship
    anything. TechCrunch's take on a launch is a normal-tier link."""
    coverage = only(
        items_from("techcrunch_rss.xml", "techcrunch"), "https://techcrunch.com/?p=99888"
    )

    assert coverage.title == "OpenAI ships a model that runs on your laptop"
    assert announces_a_launch(coverage), "the headline is an announcement"
    assert classify(coverage) is Tier.NORMAL, "but TechCrunch is not the one announcing"


@pytest.mark.parametrize(
    "title",
    [
        "OpenAI lanza un modelo que corre local",
        "Presentamos un modelo que cabe en un telefono",
        "Nuestro modelo mas pequeno ya esta disponible",
    ],
)
def test_a_launch_reads_the_same_in_spanish(title: str) -> None:
    """The feeds are mostly English, the group is not, and an accent dropped by a
    feed must not decide a tier."""
    assert classify(item(title=title, authority=1.0)) is Tier.HIGH


@pytest.mark.parametrize(
    "title",
    [
        "Por que nuestro modelo mas pequeno cambia todo",
        "Hands-on with the model everybody is talking about",
        "What the new model launch means for developers",
    ],
)
def test_talking_about_a_launch_is_never_one(title: str) -> None:
    assert classify(item(title=title, authority=1.0)) is Tier.NORMAL


def test_a_launch_from_a_lesser_source_stays_normal_tier() -> None:
    """A first-party announcement from a major AI org is two claims, and the
    authority the feed list carries is what answers the second one."""
    startup = item(title="A startup launches a voice model for radio", authority=0.45)

    assert announces_a_launch(startup)
    assert classify(startup) is Tier.NORMAL


def test_the_first_party_bar_is_a_parameter_not_a_source_list() -> None:
    """So a feed added to `rebe_agent.feeds` classifies on the weight it carries,
    and the tiers never have to know the source list exists."""
    press = item(title="TechCrunch launches its own model", authority=0.5)

    assert classify(press) is Tier.NORMAL
    assert classify(press, bar=TierBar(first_party_authority=0.5)) is Tier.HIGH


# --- what makes a remaining slot not worth keeping ---------------------------


def test_general_tech_press_nobody_upvoted_is_a_weak_candidate() -> None:
    """The mediocre link at 22:00 the pruning rule exists to stop."""
    filler = only(items_from("techcrunch_rss.xml", "techcrunch"), "https://techcrunch.com/?p=99887")

    assert score(filler, NOON) < 0.5
    assert weak(filler, NOON)


def test_a_first_party_launch_is_never_a_weak_candidate() -> None:
    launch = only(items_from("openai_atom.xml", "openai"), "https://openai.com/index/local-model")

    assert not weak(launch, NOON)


def test_a_well_upvoted_hn_story_is_not_a_weak_candidate() -> None:
    strong = only(hn_from("hn_algolia.json"), "41000001")

    assert not weak(strong, NOON)


def test_the_weak_bar_is_the_rankers_own_score_and_nothing_else() -> None:
    """No second opinion about quality: "weak" is the one transparent number the
    curator already computes, compared against a bar the posture owns."""
    filler = only(items_from("techcrunch_rss.xml", "techcrunch"), "https://techcrunch.com/?p=99887")
    its_score = score(filler, NOON)

    assert weak(filler, NOON, bar=TierBar(weak_score=its_score + 0.01))
    assert not weak(filler, NOON, bar=TierBar(weak_score=its_score))
