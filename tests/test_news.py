"""The news leg end to end: the open web to the group, and the gates on the way.

Everything downstream of the network is real - the curator, the brain, the pacer,
the posted store - and only DeepSeek, Evolution and the feeds themselves are
stand-ins. Nothing waits on the clock: it is moved by hand, and the sleeping goes
through a `ManualSleeper`, so a run that would take four minutes in the group
takes an assertion here.
"""

from __future__ import annotations

import json
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
    INSTRUCTIONS,
    MAX_POST_CHARS,
    REJECTIONS_PER_RUN,
    RETRIES_PER_ITEM,
    NewsLeg,
    NewsPost,
    PostRejectedError,
    render,
)
from rebe_agent.pacer import Envelope, Pacer, SendRefusedError
from rebe_agent.posted import InMemoryPostedStore
from rebe_agent.sends import InMemorySendLog, SendKind
from rebe_agent.usage import CallType, InMemoryUsageStore
from rebe_agent.voice import emoji_count
from tests.deepseek_stub import FakeDeepSeek, json_output_response
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import GROUP, NOON, RecordingAlerter, item
from tests.test_config import COMPLETE_ENV

SEED = 20260725

ROOMY = Envelope(post_gap=(timedelta(0), timedelta(0)))
"""The post-to-post gap lifted, so a per-run cap above one can be exercised at
all. How far apart posts really sit is the pacer's business, and is asserted
there rather than re-argued here."""


def answer(text: str = "miren, salio un modelo que corre sin nube") -> dict[str, Any]:
    return json_output_response(json.dumps({"text": text}))


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


def make_pacer(
    evolution: FakeEvolution,
    clock: ManualClock,
    log: InMemorySendLog | None = None,
    *,
    envelope: Envelope | None = None,
) -> Pacer:
    """The real pacer, against a fake Evolution and a clock a test can move."""
    return Pacer(
        EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
        log or InMemorySendLog(),
        clock,
        envelope=envelope or ROOMY,
        sleeper=ManualSleeper(clock),
        rng=random.Random(SEED),
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
    pacer = make_pacer(evolution, clock, envelope=envelope)
    return NewsLeg(make_brain(settings, fake), pacer, candidates, posted, clock)


# --- the post the group sees -------------------------------------------------


def test_the_post_is_her_words_and_the_canonical_link() -> None:
    post = NewsPost(text="miren, salio un modelo que corre sin nube")

    assert render(post, LAUNCH) == (
        "miren, salio un modelo que corre sin nube\nhttps://openai.com/index/local-model"
    )


def test_the_posted_link_is_the_publishers_own_minus_the_tracking() -> None:
    """The dedup key is canonicalised - https forced, `www.` dropped, query
    reordered - and the posted link deliberately is not. A host that serves only
    `www.`, or only http, would be handed a dead link, and "never shortens a
    link" cuts both ways."""
    tracked = item(url="https://www.openai.com/index/local-model/?utm_source=rss")

    rendered = render(NewsPost(text="ya salio"), tracked)

    assert rendered.endswith("\nhttps://www.openai.com/index/local-model/")
    assert tracked.canonical_url == "https://openai.com/index/local-model"


def test_the_words_and_the_link_are_never_run_together() -> None:
    """The live post "Miren esto Un analista predice que..." came from `render`
    joining a framing word to a capitalised sentence with a bare space. There is
    one field now, so the only join left is the one before the link, and it is a
    newline."""
    rendered = render(NewsPost(text="ojo   con   lo de los libros  raros"), LAUNCH)

    words, link = rendered.split("\n")
    assert words == "ojo con lo de los libros raros"
    assert link == LAUNCH.link


def test_a_model_that_writes_its_own_link_is_refused() -> None:
    """The one hallucination the group would actually click."""
    with pytest.raises(PostRejectedError, match="link"):
        render(NewsPost(text="esta en https://openai.com/x"), LAUNCH)

    with pytest.raises(PostRejectedError, match="link"):
        render(NewsPost(text="checa openai.com para verlo"), LAUNCH)


def test_a_number_the_source_never_gave_is_refused() -> None:
    """The mechanical half of the anti-hallucination bound: the post may restate
    the source item and nothing else."""
    with pytest.raises(PostRejectedError, match="700"):
        render(NewsPost(text="son 700 millones de parametros"), LAUNCH)


def test_a_number_the_source_did_give_is_fine() -> None:
    grounded = item(title="Llama 4 sale hoy", summary="Corre en 2 GPUs.")

    assert "4" in render(NewsPost(text="salio llama 4, corre en 2 gpus"), grounded)


def test_more_than_one_emoji_is_refused() -> None:
    with pytest.raises(PostRejectedError, match="emoji"):
        render(NewsPost(text="miren 👀🔥 salio algo"), LAUNCH)


def test_one_emoji_is_the_voice_working() -> None:
    assert "👀" in render(NewsPost(text="miren esto 👀 salio algo bueno"), LAUNCH)


def test_an_empty_answer_is_not_a_post() -> None:
    with pytest.raises(PostRejectedError, match="nothing"):
        render(NewsPost(text="   "), LAUNCH)


def test_a_report_is_not_a_reaction() -> None:
    """The three posts that started this change were 77, 103 and 141 characters -
    headlines, translated. The cap is the width of the thing she is asked for."""
    with pytest.raises(PostRejectedError, match="reaction"):
        render(NewsPost(text="a" * (MAX_POST_CHARS + 1)), LAUNCH)

    reaction = "ojo con lo de los libros raros y la IA 👀"
    assert len(reaction) <= MAX_POST_CHARS
    assert render(NewsPost(text=reaction), LAUNCH).startswith(reaction)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sin nada", 0),
        ("hola 👀 que tal", 1),
        ("hola 👀 que 🔥 tal", 2),
        ("una familia 👩‍💻 programando", 1),
        ("saludos 👋🏽 desde aca", 1),
        ("viva mexico 🇲🇽", 1),
        ("mexico 🇲🇽 y espana 🇪🇸", 2),
        ("ojo ‼️ con esto", 1),
        ("el 50% de la gente, -no todos-", 0),
    ],
)
def test_emoji_are_counted_the_way_a_reader_counts_them(text: str, expected: int) -> None:
    """A flag is two code points and one picture; `‼️` is punctuation plus a
    variation selector and one picture. Counting code points would refuse
    "viva mexico 🇲🇽" for using two emoji, which is not what a reader sees."""
    assert emoji_count(text) == expected


def test_a_single_flag_is_not_two_emoji() -> None:
    assert "🇲🇽" in render(NewsPost(text="salio algo en mexico 🇲🇽"), LAUNCH)


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
        "miren, salio un modelo que corre sin nube\nhttps://openai.com/index/local-model"
    ]
    assert [row.canonical_url for row in posted.items] == ["https://openai.com/index/local-model"]


async def test_every_call_carries_the_persona(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """The regression that produced three press releases in the group: these
    instructions existed and were never passed, so the only thing a call carried
    was the source, the headline and a schema - and back came the headline."""
    fake = FakeDeepSeek(answer())

    await make_leg(settings, fake, evolution, posted, clock, StubCandidates(LAUNCH)).run(GROUP)

    request = json.dumps(fake.last_request, ensure_ascii=False)
    assert INSTRUCTIONS.splitlines()[0] in request
    assert "No la reportes" in request


async def test_she_is_shown_what_she_already_wrote(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """Memory, not a rule: the same prompt at the same temperature drifts to the
    same opener, so the second call is told how the first one read. Her own words
    go in; the link never does, for the same reason the article's never does."""
    pool = StubCandidates(
        item(source_id="a", title="Primera nota de hoy sobre IA", url="https://a.mx/1"),
        item(source_id="b", title="Segunda nota de hoy sobre IA", url="https://a.mx/2"),
    )
    fake = FakeDeepSeek(answer(), answer(text="ya salio otra cosa hoy"))

    await make_leg(settings, fake, evolution, posted, clock, pool).run(GROUP, limit=2)

    first, second = (request["messages"][-1]["content"] for request in fake.requests)
    assert "miren, salio un modelo que corre sin nube" not in first, "nothing written yet"
    assert "miren, salio un modelo que corre sin nube" in second
    assert "https://" not in second


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
        FakeDeepSeek(answer(), answer(text="otra cosa distinta paso hoy")),
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
    """Silently: a bad call is not an outage, and the item comes back next run.

    The run ends there rather than trying the next candidate, because what failed
    is the brain - the endpoint, or the day's call ceiling - and the next
    candidate would fail in exactly the same way.
    """
    fake = FakeDeepSeek(500)
    pool = StubCandidates(LAUNCH, item(source_id="b", url="https://a.mx/2"))

    sent = await make_leg(settings, fake, evolution, posted, clock, pool).run(GROUP)

    assert sent == []
    assert evolution.calls == []
    assert posted.items == []
    assert len(fake.requests) == 1, "a broken brain is not asked again this run"


async def test_a_post_that_fails_validation_never_reaches_the_group(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    fake = FakeDeepSeek(answer(text="son 900 mil millones de parametros, neta"))

    sent = await make_leg(settings, fake, evolution, posted, clock, StubCandidates(LAUNCH)).run(
        GROUP
    )

    assert sent == []
    assert evolution.calls == []
    assert posted.items == []


async def test_an_unusable_answer_asks_the_same_article_again(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """A rejection is about the wording, and the article underneath it is still
    the best thing on the shortlist. So the story is not thrown away with the
    sentence: the reason goes back and the same item is asked again."""
    best = item(source_id="a", title="Primera nota sobre un modelo", url="https://a.mx/1")
    next_best = item(source_id="b", title="Segunda nota sobre un modelo", url="https://a.mx/2")
    fake = FakeDeepSeek(answer(text="son 900 mil millones de parametros"), answer(text="ya salio"))

    sent = await make_leg(
        settings, fake, evolution, posted, clock, StubCandidates(best, next_best)
    ).run(GROUP)

    assert [post.item for post in sent] == [best], "the retry rescued the story"
    assert [row.source_id for row in posted.items] == ["a"]


async def test_the_retry_is_told_what_was_wrong_with_the_last_answer(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """Asking again with the same prompt would buy the same answer. What makes
    the second attempt different is `render`'s own words for the refusal."""
    fake = FakeDeepSeek(answer(text="son 900 mil millones de parametros"), answer(text="ya salio"))

    await make_leg(settings, fake, evolution, posted, clock, StubCandidates(LAUNCH)).run(GROUP)

    retry = fake.requests[1]["messages"][-1]["content"]
    assert "900" in retry, "the reason names the figure the source never supplied"


async def test_a_story_she_cannot_write_about_is_given_up_on(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """The retry is one second chance, not a loop: an article the model can only
    describe by inventing a number is dropped, and the run tries the next one."""
    unwritable = item(source_id="a", title="Primera nota sobre un modelo", url="https://a.mx/1")
    ordinary = item(source_id="b", title="Segunda nota sobre un modelo", url="https://a.mx/2")
    fake = FakeDeepSeek(
        *[answer(text="son 900 mil millones de parametros")] * (RETRIES_PER_ITEM + 1),
        answer(text="ya salio"),
    )

    sent = await make_leg(
        settings, fake, evolution, posted, clock, StubCandidates(unwritable, ordinary)
    ).run(GROUP)

    assert [post.item for post in sent] == [ordinary]
    assert [row.source_id for row in posted.items] == ["b"]


async def test_a_run_stops_paying_for_answers_it_keeps_rejecting(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    """Moving on has to be bounded, or a bad day spends the whole shortlist."""
    pool = StubCandidates(
        *(
            item(
                source_id=str(number),
                title=f"Nota numero {number} de hoy",
                url=f"https://a.mx/{number}",
            )
            for number in range(REJECTIONS_PER_RUN + 3)
        )
    )
    fake = FakeDeepSeek(answer(text="son 900 mil millones de parametros"))

    sent = await make_leg(settings, fake, evolution, posted, clock, pool).run(GROUP)

    assert sent == []
    assert len(fake.requests) == REJECTIONS_PER_RUN * (RETRIES_PER_ITEM + 1)


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
    fake = FakeDeepSeek(answer(), answer(text="tambien salio otra cosa"), answer(text="y otra mas"))

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

    await NewsLeg(brain, make_pacer(evolution, clock), StubCandidates(LAUNCH), posted, clock).run(
        GROUP
    )

    totals = await store.totals_on(clock.now().date())
    assert totals[CallType.NEWS_SUMMARY].calls == 1


async def test_the_send_is_recorded_as_a_post(
    settings: Settings,
    evolution: FakeEvolution,
    posted: InMemoryPostedStore,
    clock: ManualClock,
) -> None:
    log = InMemorySendLog()
    brain = make_brain(settings, FakeDeepSeek(answer()))

    await NewsLeg(
        brain, make_pacer(evolution, clock, log), StubCandidates(LAUNCH), posted, clock
    ).run(GROUP)

    latest = await log.latest()
    assert latest is not None
    assert latest.kind is SendKind.POST
    assert latest.chat == GROUP
