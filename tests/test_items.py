"""The three keys a candidate is known by, and what each one is meant to catch.

These are the whole anti-repost gate. DeepSeek has no embeddings endpoint, so
there is no semantic layer behind them: if the stable ID, the canonical URL and
the title hash all miss, the item gets posted twice. So each layer is asserted on
the exact collision it exists for.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from rebe_agent.items import canonical_url, shareable_url, title_hash
from tests.support import NOON, item

ARTICLE = "https://openai.com/index/nuevo-modelo"


@pytest.mark.parametrize(
    "raw",
    [
        "https://openai.com/index/nuevo-modelo",
        "http://openai.com/index/nuevo-modelo",
        "https://OpenAI.com/index/nuevo-modelo",
        "https://www.openai.com/index/nuevo-modelo",
        "https://openai.com/index/nuevo-modelo/",
        "https://openai.com/index/nuevo-modelo#hero",
        "  https://openai.com/index/nuevo-modelo  ",
        "https://openai.com/index/nuevo-modelo?utm_source=hn&utm_medium=social",
        "https://openai.com/index/nuevo-modelo?fbclid=abc123",
        "https://openai.com/index/nuevo-modelo?ref=hackernews",
    ],
)
def test_the_same_article_dressed_ten_ways_is_one_url(raw: str) -> None:
    """The collapse the posted store depends on: HN and a vendor feed carrying
    the same launch, with the tracking each of them bolts on, is one key."""
    assert canonical_url(raw) == ARTICLE


def test_a_query_the_page_actually_needs_survives() -> None:
    """Stripping is limited to known trackers: `?id=` is the article, not a tag."""
    assert canonical_url("https://example.mx/post?id=42&utm_campaign=x") == (
        "https://example.mx/post?id=42"
    )


def test_query_order_does_not_make_a_second_article() -> None:
    assert canonical_url("https://example.mx/p?b=2&a=1") == canonical_url(
        "https://example.mx/p?a=1&b=2"
    )


def test_a_non_default_port_is_part_of_the_address() -> None:
    assert canonical_url("http://example.mx:8080/p") == "https://example.mx:8080/p"


@pytest.mark.parametrize(
    "raw", ["", "   ", "not a url", "/relative/path", "mailto:rebe@bien.mx", "ftp://example.mx/x"]
)
def test_an_unusable_link_canonicalises_to_nothing(raw: str) -> None:
    """Answering "" rather than raising is what lets the freshness filter drop
    these in the same pass as a missing title, without exception flow."""
    assert canonical_url(raw) == ""


def test_the_link_a_reader_follows_keeps_the_address_the_publisher_wrote() -> None:
    """The canonical form is a comparison key and would be a bad link: forcing
    https and dropping `www.` are exactly the edits that turn a working address
    into a 404 on a host that serves only one of them."""
    raw = "https://www.openai.com/index/local-model/?utm_source=hn#hero"

    assert shareable_url(raw) == "https://www.openai.com/index/local-model/"
    assert canonical_url(raw) == "https://openai.com/index/local-model"


def test_a_shared_link_keeps_its_own_scheme() -> None:
    assert shareable_url("http://ejemplo.mx/nota") == "http://ejemplo.mx/nota"


def test_a_shared_link_keeps_the_parameters_the_page_needs_in_the_order_it_had() -> None:
    assert shareable_url("https://ejemplo.mx/p?b=2&a=1&fbclid=xyz") == (
        "https://ejemplo.mx/p?b=2&a=1"
    )


@pytest.mark.parametrize("raw", ["", "not a url", "mailto:rebe@bien.mx"])
def test_an_unusable_link_is_never_shared(raw: str) -> None:
    assert shareable_url(raw) == ""


def test_the_same_headline_reposted_elsewhere_hashes_the_same() -> None:
    """The layer that catches a syndicated repost: different host, same story."""
    first = item(title="OpenAI lanza un modelo que corre local", url="https://a.mx/x")
    second = item(title="  openai   lanza un modelo que corre local!  ", url="https://b.mx/y")

    assert first.canonical_url != second.canonical_url
    assert first.title_hash == second.title_hash


def test_accents_and_punctuation_do_not_make_a_new_story() -> None:
    assert title_hash("Un modelo más rápido, ya") == title_hash("un modelo mas rapido ya")


def test_two_different_stories_do_not_collide() -> None:
    assert title_hash("OpenAI lanza un modelo") != title_hash("Google lanza un modelo")


def test_an_item_carries_both_its_key_and_its_link() -> None:
    candidate = item(
        source="hackernews",
        source_id="41234567",
        title="Modelo nuevo",
        url="https://openai.com/index/x?utm_source=hn",
        published_at=NOON - timedelta(hours=2),
    )

    assert candidate.source_key == ("hackernews", "41234567")
    assert candidate.canonical_url == "https://openai.com/index/x"
    assert candidate.title_hash == title_hash("Modelo nuevo")
    assert candidate.link == "https://openai.com/index/x"


def test_grounding_is_everything_the_framing_line_may_restate() -> None:
    """The bound from the reply policy is enforced against this text and nothing
    else, so it is one property rather than a rule each caller reassembles."""
    candidate = item(title="Sale GPT-5", summary="Corre en 2 GPUs.")

    assert "Sale GPT-5" in candidate.grounding
    assert "Corre en 2 GPUs." in candidate.grounding
