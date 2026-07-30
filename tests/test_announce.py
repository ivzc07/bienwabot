"""The announcement twin: a high-tier post, restated for the Announcements channel.

Everything downstream of the network is real - the tier classification, the
brain, the pacer, the render gates - and only DeepSeek and Evolution are
stand-ins, so what is asserted is the channel's view: which room each message
landed in, and that a twin that could not go out never costs the group its post.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest

from rebe_agent.announce import (
    INSTRUCTIONS,
    MAX_ANNOUNCEMENT_CHARS,
    Announcement,
    AnnouncementRejectedError,
    Announcer,
    render,
)
from rebe_agent.brain import Brain, build_brain
from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.config import Settings, load_settings
from rebe_agent.evolution import EvolutionClient
from rebe_agent.items import NewsItem
from rebe_agent.news import NewsLeg
from rebe_agent.pacer import Envelope, Pacer, RefusalReason, SendRefusedError
from rebe_agent.posted import InMemoryPostedStore
from rebe_agent.sends import InMemorySendLog, SendKind
from rebe_agent.usage import InMemoryUsageStore
from tests.deepseek_stub import FakeDeepSeek, json_output_response
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import GROUP, MEXICO_CITY, NOON, RecordingAlerter, item
from tests.test_config import COMPLETE_ENV

SEED = 20260730

CHANNEL = "120363000000000099@g.us"
"""The bien.mx Announcements channel, as far as any test is concerned."""

ROOMY = Envelope(post_gap=(timedelta(0), timedelta(0)))
"""The post-to-post gap lifted where a test is not about the envelope."""

LAUNCH = item(
    source="openai",
    source_id="openai-1",
    title="OpenAI lanza un modelo que corre local",
    url="https://openai.com/index/local-model",
    summary="Corre sin nube.",
    published_at=NOON - timedelta(hours=2),
)
"""First-party authority and a launch headline: high tier by the second bar."""

COMMENTARY = item(
    source="openai",
    source_id="openai-2",
    title="Reflexiones sobre el futuro de la IA",
    url="https://openai.com/index/reflexiones",
    published_at=NOON - timedelta(hours=2),
)
"""Same source, no launch in the headline: normal tier, so no twin."""

PROFESSIONAL = "OpenAI presentó un modelo que funciona sin conexión, disponible desde hoy."


def group_answer(text: str = "ya salio el modelo que corre sin nube") -> dict[str, Any]:
    return json_output_response(json.dumps({"for_the_group": True, "text": text}))


def channel_answer(text: str = PROFESSIONAL) -> dict[str, Any]:
    return json_output_response(json.dumps({"text": text}))


class StubCandidates:
    """A fixed pool; `post_one` is handed items directly, so it stays empty."""

    async def fetch(self, now: datetime) -> Sequence[NewsItem]:
        return []


@pytest.fixture
def settings() -> Settings:
    return load_settings(dict(COMPLETE_ENV))


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOON)


@pytest.fixture
def evolution() -> FakeEvolution:
    return FakeEvolution()


def make_brain(settings: Settings, fake: FakeDeepSeek) -> Brain:
    return build_brain(
        settings,
        ManualClock(NOON),
        InMemoryUsageStore(),
        RecordingAlerter(),
        http_client=fake.client(),
    )


def make_pacer(
    evolution: FakeEvolution, clock: ManualClock, *, envelope: Envelope | None = None
) -> Pacer:
    return Pacer(
        EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
        InMemorySendLog(),
        clock,
        envelope=envelope or ROOMY,
        sleeper=ManualSleeper(clock),
        rng=random.Random(SEED),
    )


def make_leg(
    settings: Settings,
    fake: FakeDeepSeek,
    pacer: Pacer,
    *,
    announcer: Announcer | None = None,
) -> NewsLeg:
    return NewsLeg(
        make_brain(settings, fake),
        pacer,
        StubCandidates(),
        InMemoryPostedStore(),
        ManualClock(NOON),
        announcer=announcer,
    )


def leg_with_channel(
    settings: Settings,
    fake: FakeDeepSeek,
    evolution: FakeEvolution,
    clock: ManualClock,
    *,
    envelope: Envelope | None = None,
) -> NewsLeg:
    """The wiring `build_news_stack` does: one brain and one pacer for both rooms."""
    brain = make_brain(settings, fake)
    pacer = make_pacer(evolution, clock, envelope=envelope)
    return NewsLeg(
        brain,
        pacer,
        StubCandidates(),
        InMemoryPostedStore(),
        clock,
        announcer=Announcer(brain, pacer, CHANNEL),
    )


def chats(evolution: FakeEvolution) -> list[str]:
    """Which room each text landed in, in order."""
    return [str(call.body.get("number", "")) for call in evolution.calls if call.is_text]


# --- the message the channel sees --------------------------------------------


def test_the_announcement_is_the_words_and_the_publishers_link() -> None:
    rendered = render(Announcement(text=PROFESSIONAL), LAUNCH)

    assert rendered == f"{PROFESSIONAL}\nhttps://openai.com/index/local-model"


def test_the_professional_register_carries_no_emoji_at_all() -> None:
    """The group allows one; the channel allows none - a stray 👀 in an
    announcements channel is a persona leak, not a person."""
    with pytest.raises(AnnouncementRejectedError, match="emoji"):
        render(Announcement(text="OpenAI presentó un modelo nuevo 👀"), LAUNCH)


def test_a_model_that_writes_its_own_link_is_refused() -> None:
    with pytest.raises(AnnouncementRejectedError, match="link"):
        render(Announcement(text="Los detalles están en https://openai.com/x"), LAUNCH)


def test_an_invented_number_is_refused() -> None:
    with pytest.raises(AnnouncementRejectedError, match="never mentions"):
        render(Announcement(text="El modelo tiene 700 mil millones de parámetros"), LAUNCH)


def test_a_bulletin_over_the_cap_is_refused() -> None:
    long_way_past = "El modelo nuevo de OpenAI corre local y sin nube. " * 6
    with pytest.raises(AnnouncementRejectedError, match=str(MAX_ANNOUNCEMENT_CHARS)):
        render(Announcement(text=long_way_past), LAUNCH)


def test_an_empty_answer_is_refused() -> None:
    with pytest.raises(AnnouncementRejectedError, match="nothing"):
        render(Announcement(text=" : "), LAUNCH)


# --- which room gets what ----------------------------------------------------


async def test_a_high_tier_post_gets_its_twin_in_the_channel(
    settings: Settings, evolution: FakeEvolution, clock: ManualClock
) -> None:
    fake = FakeDeepSeek(group_answer(), channel_answer())
    leg = leg_with_channel(settings, fake, evolution, clock)

    await leg.post_one(GROUP, LAUNCH)

    assert chats(evolution) == [GROUP, CHANNEL]
    assert evolution.texts[1] == f"{PROFESSIONAL}\n{LAUNCH.link}"
    # The second call is the professional register, not the group prompt again.
    second = json.dumps(fake.requests[1], ensure_ascii=False)
    assert INSTRUCTIONS.splitlines()[0] in second


async def test_a_normal_item_posts_without_a_twin(
    settings: Settings, evolution: FakeEvolution, clock: ManualClock
) -> None:
    fake = FakeDeepSeek(group_answer("ojo con lo que dice openai del futuro"))
    leg = leg_with_channel(settings, fake, evolution, clock)

    await leg.post_one(GROUP, COMMENTARY)

    assert chats(evolution) == [GROUP]
    assert len(fake.requests) == 1


async def test_no_channel_configured_means_no_twin(
    settings: Settings, evolution: FakeEvolution, clock: ManualClock
) -> None:
    """Unset `REBE_ANNOUNCE_JID` is the behaviour from before the spec."""
    fake = FakeDeepSeek(group_answer())
    leg = make_leg(settings, fake, make_pacer(evolution, clock), announcer=None)

    await leg.post_one(GROUP, LAUNCH)

    assert chats(evolution) == [GROUP]
    assert len(fake.requests) == 1


# --- every failure is a logged nothing ---------------------------------------


async def test_a_brain_failure_costs_the_channel_and_not_the_group(
    settings: Settings, evolution: FakeEvolution, clock: ManualClock
) -> None:
    fake = FakeDeepSeek(group_answer(), 500)
    leg = leg_with_channel(settings, fake, evolution, clock)

    posted = await leg.post_one(GROUP, LAUNCH)

    assert posted.item is LAUNCH
    assert chats(evolution) == [GROUP]


async def test_a_rejected_answer_costs_the_channel_and_not_the_group(
    settings: Settings, evolution: FakeEvolution, clock: ManualClock
) -> None:
    """One try: a twin the rules refuse is dropped, never rewritten."""
    fake = FakeDeepSeek(group_answer(), channel_answer("Un anuncio con emoji 🚀"))
    leg = leg_with_channel(settings, fake, evolution, clock)

    posted = await leg.post_one(GROUP, LAUNCH)

    assert posted.item is LAUNCH
    assert chats(evolution) == [GROUP]
    assert len(fake.requests) == 2


async def test_a_refused_send_costs_the_channel_and_not_the_group(
    settings: Settings, evolution: FakeEvolution, clock: ManualClock
) -> None:
    """The twin spends the same daily allowance as everything else from the
    number, and hitting the ceiling is a logged nothing rather than an error."""
    fake = FakeDeepSeek(group_answer(), channel_answer())
    leg = leg_with_channel(
        settings,
        fake,
        evolution,
        clock,
        envelope=Envelope(post_gap=(timedelta(0), timedelta(0)), sends_per_day=1),
    )

    posted = await leg.post_one(GROUP, LAUNCH)

    assert posted.item is LAUNCH
    assert chats(evolution) == [GROUP]


# --- how the envelope treats the twin ----------------------------------------


async def test_the_twin_skips_the_post_to_post_gap(
    evolution: FakeEvolution, clock: ManualClock
) -> None:
    """It lands moments after its group twin, the way a person forwards their
    own message to a second chat - the 75-90 minute gap is for the group."""
    pacer = make_pacer(evolution, clock, envelope=Envelope())

    await pacer.send(SendKind.POST, GROUP, "ya salio el modelo\nhttps://openai.com/x")
    await pacer.send(SendKind.ANNOUNCEMENT, CHANNEL, f"{PROFESSIONAL}\nhttps://openai.com/x")

    assert chats(evolution) == [GROUP, CHANNEL]


async def test_the_twin_obeys_the_overnight_hold(evolution: FakeEvolution) -> None:
    """Nothing scheduled leaves at 23:30, whatever room it is bound for."""
    late = ManualClock(datetime(2026, 7, 25, 23, 30, tzinfo=MEXICO_CITY))
    pacer = make_pacer(evolution, late, envelope=Envelope())

    with pytest.raises(SendRefusedError) as refusal:
        await pacer.send(SendKind.ANNOUNCEMENT, CHANNEL, f"{PROFESSIONAL}\nhttps://openai.com/x")

    assert refusal.value.reason is RefusalReason.OVERNIGHT_HOLD
    assert chats(evolution) == []


async def test_the_twin_does_not_move_the_next_posts_gap(
    evolution: FakeEvolution, clock: ManualClock
) -> None:
    """The gap is measured post to post: an announcement in between neither
    resets it nor counts as the post it follows."""
    log = InMemorySendLog()
    pacer = Pacer(
        EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client()),
        log,
        clock,
        envelope=Envelope(),
        sleeper=ManualSleeper(clock),
        rng=random.Random(SEED),
    )
    await pacer.send(SendKind.POST, GROUP, "primero\nhttps://openai.com/a")
    await pacer.send(SendKind.ANNOUNCEMENT, CHANNEL, "El primero, anunciado.\nhttps://openai.com/a")

    clock.advance(timedelta(minutes=95))
    await pacer.send(SendKind.POST, GROUP, "segundo\nhttps://openai.com/b")

    assert chats(evolution) == [GROUP, CHANNEL, GROUP]
