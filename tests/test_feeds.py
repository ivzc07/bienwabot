"""Reading the two source layers, against recorded responses rather than the web.

Fixtures, not live calls, on purpose: a test that fetched Hacker News would fail
on the day HN was slow and would silently pass on the day it started answering
something else. What is asserted here is that the shapes those APIs *do* return
become candidates, and that one bad source never costs the run its post.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rebe_agent.curate import Filters, usable
from rebe_agent.feeds import (
    FEEDS,
    HN_ENDPOINT,
    Feed,
    WebCandidates,
    feed_items,
    hacker_news_items,
)
from rebe_agent.items import NewsItem
from tests.support import NOON, fixture

OPENAI = Feed("openai", "OpenAI", "https://openai.com/news/rss.xml", 1.0)
TECHCRUNCH = Feed("techcrunch", "TechCrunch", "https://techcrunch.com/feed/", 0.5)


def hn_items() -> list[NewsItem]:
    return hacker_news_items(json.loads(fixture("hn_algolia.json")))


def only(items: list[NewsItem], source_id: str) -> NewsItem:
    found = [item for item in items if item.source_id == source_id]
    assert len(found) == 1, f"expected exactly one {source_id}, got {len(found)}"
    return found[0]


def test_an_algolia_response_becomes_hydrated_candidates() -> None:
    """One request, everything the ranker needs: no second call per story."""
    launch = only(hn_items(), "41000001")

    assert launch.source == "hackernews"
    assert launch.title == "OpenAI releases a model that runs on your laptop"
    assert launch.canonical_url == "https://openai.com/index/local-model"
    assert launch.points == 640
    assert launch.comments == 210
    assert launch.published_at == datetime(2026, 7, 25, 14, 0, tzinfo=UTC)


def test_an_ask_hn_thread_has_no_article_to_link() -> None:
    """`url: null` is a text post. It parses, and then the filter drops it -
    rather than being special-cased in two places."""
    ask = only(hn_items(), "41000002")

    assert ask.url == ""
    assert not usable(ask, NOON)


def test_a_story_below_the_points_floor_never_reaches_the_model() -> None:
    """Algolia is asked to filter, and the answer is checked anyway: a floor that
    only lives in a query string is a floor one typo removes."""
    quiet = only(hn_items(), "41000003")

    assert quiet.points == 45
    assert not usable(quiet, NOON)


def test_hn_text_is_handed_over_as_prose_not_markup() -> None:
    show = only(hn_items(), "41000004")

    assert show.summary == "It transcribes norteno radio."


def test_an_atom_entry_becomes_a_candidate() -> None:
    launch = only(
        feed_items(OPENAI, fixture("openai_atom.xml")), "https://openai.com/index/local-model"
    )

    assert launch.source == "openai"
    assert launch.authority == 1.0
    assert launch.title == "OpenAI ships a model that runs on your laptop"
    assert launch.published_at == datetime(2026, 7, 25, 13, 30, tzinfo=UTC)
    assert launch.summary == "The model runs locally, with no cloud round trip."


def test_the_alternate_link_is_the_article_not_the_feed() -> None:
    """Atom entries carry several links. The `self` one is the feed itself, and
    posting it would send the group an XML document."""
    launch = feed_items(OPENAI, fixture("openai_atom.xml"))[0]

    assert launch.canonical_url == "https://openai.com/index/local-model"


def test_an_entry_with_no_date_is_not_guessed_at() -> None:
    """Freshness is the first quality filter; an item with no timestamp cannot be
    judged by it, and dating it "now" would put it straight to the top."""
    items = feed_items(OPENAI, fixture("openai_atom.xml"))

    assert [item.source_id for item in items] == ["https://openai.com/index/local-model"]


def test_an_rss_item_becomes_a_candidate() -> None:
    items = feed_items(TECHCRUNCH, fixture("techcrunch_rss.xml"))
    startup = items[0]

    assert startup.source_id == "https://techcrunch.com/?p=99887"
    assert startup.canonical_url == "https://techcrunch.com/2026/07/25/startup-voz-ia"
    assert startup.published_at == datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
    assert startup.summary == "La ronda es de 20 millones de dolares."
    assert startup.points is None


def test_a_feed_that_will_not_parse_costs_one_source_not_the_run() -> None:
    assert feed_items(OPENAI, b"<rss><channel><item>") == []
    assert feed_items(OPENAI, b"") == []


def test_the_shipped_feed_set_is_the_one_the_research_verified() -> None:
    """The unverified feeds (The Verge, Ars Technica) and the ones with no
    first-party feed at all (Anthropic, Meta AI) are deliberately absent."""
    keys = {feed.key for feed in FEEDS}

    assert {"openai", "deepmind", "googleai", "huggingface"} <= keys
    assert not {"anthropic", "meta", "theverge", "arstechnica"} & keys
    assert all(0.0 <= feed.authority <= 1.0 for feed in FEEDS)


class Web:
    """An httpx transport that answers from fixtures and remembers the requests."""

    def __init__(self, *, failing: str = "") -> None:
        self.requests: list[httpx.Request] = []
        self._failing = failing

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if self._failing and self._failing in str(request.url):
            return httpx.Response(503, text="down")
        if host == "hn.algolia.com":
            return httpx.Response(200, content=fixture("hn_algolia.json"))
        if host == "openai.com":
            return httpx.Response(200, content=fixture("openai_atom.xml"))
        return httpx.Response(200, content=fixture("techcrunch_rss.xml"))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    @property
    def hn_query(self) -> dict[str, str]:
        for request in self.requests:
            if str(request.url).startswith(HN_ENDPOINT):
                return dict(request.url.params)
        raise AssertionError("Hacker News was never asked")


async def test_the_pool_is_every_source_that_answered() -> None:
    web = Web()

    pool = await WebCandidates(web.client(), feeds=[OPENAI, TECHCRUNCH]).fetch(NOON)

    assert {item.source for item in pool} == {"hackernews", "openai", "techcrunch"}


async def test_the_floor_and_the_window_are_pushed_into_the_hn_query() -> None:
    """Filtering at the source is what keeps this to one request per run."""
    web = Web()

    await WebCandidates(
        web.client(),
        feeds=[],
        filters=Filters(points_floor=150, freshness=timedelta(hours=24)),
    ).fetch(NOON)

    params = web.hn_query
    since = int((NOON - timedelta(hours=24)).timestamp())
    assert params["tags"] == "story"
    assert params["numericFilters"] == f"points>=150,created_at_i>={since}"


@pytest.mark.parametrize("dead", ["hn.algolia.com", "openai.com"])
async def test_a_dead_source_does_not_cost_the_run_its_post(dead: str) -> None:
    web = Web(failing=dead)

    pool = await WebCandidates(web.client(), feeds=[OPENAI, TECHCRUNCH]).fetch(NOON)

    assert pool
    assert dead not in {item.source for item in pool}
