"""The news leg end to end: the open web to the group, and the gates on the way.

Everything downstream of the network is real - the curator, the brain, the pacer,
the posted store - and only DeepSeek, Evolution and the feeds themselves are
stand-ins. Nothing waits on the clock: it is moved by hand, and the sleeping goes
through a `ManualSleeper`, so a run that would take four minutes in the group
takes an assertion here.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest

from rebe_agent.brain import Brain, build_brain
from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.config import Settings, load_settings
from rebe_agent.evolution import EvolutionClient, EvolutionError
from rebe_agent.items import NewsItem
from rebe_agent.news import (
    MAX_FRAMING_CHARS,
    NewsLeg,
    NewsPost,
    PostRejectedError,
    emoji_count,
    render,
)
from rebe_agent.pacer import Envelope, Pacer, SendRefusedError
from rebe_agent.posted import InMemoryPostedStore
from rebe_agent.sends import InMemorySendLog, SendKind
from rebe_agent.usage import CallType, InMemoryUsageStore
from tests.deepseek_stub import FakeDeepSeek, tool_call_response
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import GROUP, NOON, RecordingAlerter, item

SEED = 20260725

ROOMY = Envelope(post_gap=(timedelta(0), timedelta(0)))
"""The post-to-post gap lifted, so a per-run cap above one can be exercised at
all. How far apart posts really sit is the pacer's business, and is asserted
there rather than re-argued here."""


def answer(
    opener: str = "miren", line: str = "salio un modelo que corre en tu compu"
) -> dict[str, Any]:
    return tool_call_response(f'{{"opener": {opener!r}, "line": {line!r}}}'.replace("'", '"'))


LAUNCH = item(
    source="openai",
    source_id="openai-1",
    title="OpenAI lanza un modelo que corre local",
    url="https://openai.com/index/local-model",
    summary="Corre sin nube.",
    published_at=NOON - timedelta(hours=2),
)


class StubCandidates:
    """A fixed pool, and a count of how often it was asked for one."""

    def __init__(self, *items: NewsItem) -> None:
        self.items = list(items)
        self.runs = 0

    async def fetch(self, now: datetime) -> Sequence[NewsItem]:
        self.runs += 1
        return list(self.items)


@pytest.fixture
def settings() -> Settings:
    from tests.test_config import COMPLETE_ENV

    return load_settings(dict(COMPLETE_ENV))


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOON)


@pytest.fixture
def evolution() -> FakeEvolution:
    return FakeEvolution()


@pytest.fixture
def posted() -> InMemoryPostedStore:
    return InMemoryPostedStore()


def make_brain(settings: Settings, fake: FakeDeepSeek) -> Brain:
    return build_brain(
        settings,
        ManualClock(NOON),
        InMemoryUsageStore(),
        RecordingAlerter(),
        http_client=fake.client(),
    )


def make_leg(
    settings: Settings,
    fake: FakeDeepSeek,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
    candidates: StubCandidates,
    *,
    envelope: Envelope | None = None,
) -> NewsLeg:
    pacer = Pacer(
        EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
        InMemorySendLog(),
        clock,
        envelope=envelope or ROOMY,
        sleeper=ManualSleeper(clock),
        rng=random.Random(SEED),
    )
    return NewsLeg(make_brain(settings, fake), pacer, candidates, posted, clock)


# --- the post the group sees -------------------------------------------------


def test_the_post_is_one_framing_line_and_the_canonical_link() -> None:
    post = NewsPost(opener="miren", line="salio un modelo que corre en tu compu")

    assert render(post, LAUNCH) == (
        "miren salio un modelo que corre en tu compu\nhttps://openai.com/index/local-model"
    )


def test_the_link_is_the_canonical_one_not_whatever_the_feed_carried() -> None:
    tracked = item(url="https://www.openai.com/index/local-model/?utm_source=rss")

    rendered = render(NewsPost(opener="", line="ya salio"), tracked)

    assert rendered.endswith("\nhttps://openai.com/index/local-model")


def test_a_post_with_no_framing_word_is_still_a_post() -> None:
    """The persona spec asks for the framing word to be rotated, and sometimes
    left out entirely - so an empty opener is a valid answer, not a failure."""
    assert render(NewsPost(opener="", line="ya salio el modelo"), LAUNCH).startswith(
        "ya salio el modelo\n"
    )


def test_a_model_that_writes_its_own_link_is_refused() -> None:
    """The one hallucination the group would actually click."""
    with pytest.raises(PostRejectedError, match="link"):
        render(NewsPost(opener="miren", line="esta en https://openai.com/x"), LAUNCH)

    with pytest.raises(PostRejectedError, match="link"):
        render(NewsPost(opener="miren", line="checa openai.com para verlo"), LAUNCH)


def test_a_number_the_source_never_gave_is_refused() -> None:
    """The mechanical half of the anti-hallucination bound: the framing line may
    restate the source item and nothing else."""
    with pytest.raises(PostRejectedError, match="700"):
        render(NewsPost(opener="miren", line="son 700 millones de parametros"), LAUNCH)


def test_a_number_the_source_did_give_is_fine() -> None:
    grounded = item(title="Llama 4 sale hoy", summary="Corre en 2 GPUs.")

    assert "4" in render(NewsPost(opener="miren", line="salio llama 4, corre en 2 gpus"), grounded)


def test_more_than_one_emoji_is_refused() -> None:
    with pytest.raises(PostRejectedError, match="emoji"):
        render(NewsPost(opener="miren 👀🔥", line="salio algo"), LAUNCH)


def test_one_emoji_is_the_voice_working() -> None:
    assert "👀" in render(NewsPost(opener="miren esto 👀", line="salio algo bueno"), LAUNCH)


def test_an_empty_line_is_not_a_post() -> None:
    with pytest.raises(PostRejectedError, match="no line"):
        render(NewsPost(opener="miren", line="   "), LAUNCH)


def test_an_essay_is_not_a_whatsapp_message() -> None:
    with pytest.raises(PostRejectedError, match="characters"):
        render(NewsPost(opener="", line="a" * (MAX_FRAMING_CHARS + 1)), LAUNCH)


def test_a_joined_emoji_sequence_counts_as_the_one_emoji_it_looks_like() -> None:
    assert emoji_count("hola 👩‍💻 que tal") == 1
    assert emoji_count("hola 👀 que 🔥 tal") == 2
    assert emoji_count("sin nada") == 0


# --- one run -----------------------------------------------------------------


async def test_one_run_puts_one_curated_item_in_the_group(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """The headline acceptance criterion: fetch, curate, summarise, post."""
    fake = FakeDeepSeek(answer())
    candidates = StubCandidates(LAUNCH)

    sent = await make_leg(settings, fake, evolution, posted, clock, candidates).run(GROUP)

    assert [post.item for post in sent] == [LAUNCH]
    assert evolution.shape == ["composing", "text", "paused"]
    assert evolution.texts == [
        "miren salio un modelo que corre en tu compu\nhttps://openai.com/index/local-model"
    ]
    assert [row.canonical_url for row in posted.items] == ["https://openai.com/index/local-model"]


async def test_the_item_goes_out_as_a_post_so_the_quiet_hours_apply(
    settings: Settings, evolution: FakeEvolution, posted: InMemoryPostedStore
) -> None:
    """Not a reply: a scheduled post is held overnight, and this is what makes
    the news leg obey that rather than the reply rules."""
    clock = ManualClock(NOON.replace(hour=2))
    leg = make_leg(
        settings,
        FakeDeepSeek(answer()),
        evolution,
        posted,
        clock,
        StubCandidates(item(published_at=clock.now() - timedelta(hours=1))),
        envelope=Envelope(),
    )

    with pytest.raises(SendRefusedError, match="overnight_hold"):
        await leg.run(GROUP)

    assert evolution.calls == []
    assert posted.items == []


async def test_running_again_immediately_posts_nothing(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """The anti-repost gate, from the outside: the same command twice in a row."""
    fake = FakeDeepSeek(answer())
    candidates = StubCandidates(LAUNCH)
    leg = make_leg(settings, fake, evolution, posted, clock, candidates)

    await leg.run(GROUP)
    again = await leg.run(GROUP)

    assert again == []
    assert candidates.runs == 2, "the second run still looked; it just found nothing new"
    assert len(evolution.texts) == 1
    assert len(fake.requests) == 1, "the second run spent no tokens on a known item"


async def test_the_same_article_from_two_sources_is_posted_once(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """HN and the vendor's own feed carry the same launch. Within one run the
    curator collapses them; across runs the canonical URL in the store does."""
    from_hn = item(
        source="hackernews",
        source_id="41000001",
        title="OpenAI ships a local model",
        url="https://openai.com/index/local-model?utm_source=hn",
        points=640,
        authority=0.7,
    )
    leg = make_leg(
        settings,
        FakeDeepSeek(answer(), answer(line="otra cosa distinta paso hoy")),
        evolution,
        posted,
        clock,
        StubCandidates(LAUNCH, from_hn),
    )

    first = await leg.run(GROUP, limit=2)
    second = await leg.run(GROUP, limit=2)

    assert len(first) == 1
    assert second == []


async def test_a_repost_under_a_different_url_is_caught_by_the_title(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    syndicated = item(
        source="venturebeat",
        source_id="vb-9",
        title="OpenAI lanza un modelo que corre local",
        url="https://venturebeat.com/2026/07/25/openai-local",
    )
    fake = FakeDeepSeek(answer())
    leg = make_leg(settings, fake, evolution, posted, clock, StubCandidates(LAUNCH))
    await leg.run(GROUP)

    later = make_leg(settings, fake, evolution, posted, clock, StubCandidates(syndicated))

    assert await later.run(GROUP) == []
    assert len(evolution.texts) == 1


async def test_stale_and_broken_candidates_never_cost_a_call(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """Every free filter runs before the model does."""
    fake = FakeDeepSeek(answer())
    pool = StubCandidates(
        item(source_id="stale", published_at=NOON - timedelta(days=4)),
        item(source_id="broken", url="not-a-url"),
        item(source_id="untitled", title="AI"),
        item(source="hackernews", source_id="quiet", points=12, url="https://a.mx/q"),
    )

    sent = await make_leg(settings, fake, evolution, posted, clock, pool).run(GROUP)

    assert sent == []
    assert fake.requests == [], "no candidate survived, so nothing was worth asking about"
    assert evolution.calls == []


async def test_a_deepseek_failure_posts_nothing_and_keeps_the_item(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """Silently: a bad call is not an outage, and the item comes back next run."""
    fake = FakeDeepSeek(500)

    sent = await make_leg(settings, fake, evolution, posted, clock, StubCandidates(LAUNCH)).run(
        GROUP
    )

    assert sent == []
    assert evolution.calls == []
    assert posted.items == []


async def test_a_post_that_fails_validation_never_reaches_the_group(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    fake = FakeDeepSeek(answer(line="son 900 mil millones de parametros, neta"))

    sent = await make_leg(settings, fake, evolution, posted, clock, StubCandidates(LAUNCH)).run(
        GROUP
    )

    assert sent == []
    assert evolution.calls == []
    assert posted.items == []


async def test_a_failed_send_does_not_permanently_burn_the_item(
    settings: Settings, posted: InMemoryPostedStore, clock: ManualClock
) -> None:
    """The store is written after the send, so an item nobody ever saw stays
    postable - which is the whole reason for that order."""
    broken = FakeEvolution()
    broken.text_status = 500
    fake = FakeDeepSeek(answer())

    with pytest.raises(EvolutionError):
        await make_leg(settings, fake, broken, posted, clock, StubCandidates(LAUNCH)).run(GROUP)

    assert posted.items == []

    working = FakeEvolution()
    sent = await make_leg(settings, fake, working, posted, clock, StubCandidates(LAUNCH)).run(GROUP)

    assert len(sent) == 1
    assert len(posted.items) == 1


async def test_the_per_run_cap_limits_how_many_items_a_run_can_post(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """A busy news day is still a normal day in the group."""
    pool = StubCandidates(
        item(source_id="a", title="Primera nota de hoy sobre IA", url="https://a.mx/1"),
        item(source_id="b", title="Segunda nota de hoy sobre IA", url="https://a.mx/2"),
        item(source_id="c", title="Tercera nota de hoy sobre IA", url="https://a.mx/3"),
    )
    fake = FakeDeepSeek(answer(), answer(line="tambien salio otra cosa"), answer(line="y otra mas"))

    sent = await make_leg(settings, fake, evolution, posted, clock, pool).run(GROUP, limit=2)

    assert len(sent) == 2
    assert len(fake.requests) == 2, "the third candidate never cost a call"
    assert len(posted.items) == 2


async def test_every_send_is_counted_against_the_usage_the_budget_watches(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """The news summary is call type A in the token budget spec, and lands there."""
    store = InMemoryUsageStore()
    brain = build_brain(
        settings,
        clock,
        store,
        RecordingAlerter(),
        http_client=FakeDeepSeek(answer()).client(),
    )
    pacer = Pacer(
        EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
        InMemorySendLog(),
        clock,
        envelope=ROOMY,
        sleeper=ManualSleeper(clock),
        rng=random.Random(SEED),
    )

    await NewsLeg(brain, pacer, StubCandidates(LAUNCH), posted, clock).run(GROUP)

    totals = await store.totals_on(clock.now().date())
    assert totals[CallType.NEWS_SUMMARY].calls == 1


async def test_the_send_is_recorded_as_a_post(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    log = InMemorySendLog()
    pacer = Pacer(
        EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
        log,
        clock,
        envelope=ROOMY,
        sleeper=ManualSleeper(clock),
        rng=random.Random(SEED),
    )
    brain = make_brain(settings, FakeDeepSeek(answer()))

    await NewsLeg(brain, pacer, StubCandidates(LAUNCH), posted, clock).run(GROUP)

    latest = await log.latest()
    assert latest is not None
    assert latest.kind is SendKind.POST
    assert latest.chat == GROUP
