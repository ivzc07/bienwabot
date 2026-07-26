"""The webhook leg end to end: an addressed message becomes a paced reply.

Everything downstream of the network is real - the gate, the brain, the memory,
the pacer - and only DeepSeek and Evolution are stand-ins. Nothing waits on the
clock: it is moved by hand and the sleeping goes through a `ManualSleeper`, so a
reply that takes four seconds in the group takes an assertion here.

The recurring shape of these tests is `evolution.shape`, the wire sequence as
short labels. `["read", "composing", "text", "paused"]` is the whole behavioural
claim of this leg in one line: she read it first, she looked like she was typing,
then she answered, then she stopped looking like it.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import timedelta
from typing import Any

import pytest

from rebe_agent.brain import Brain, build_brain
from rebe_agent.chimeins import CHIME_INS_PER_DAY, COOLDOWN, ChimeInBudget, InMemoryChimeInLog
from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.config import Settings, load_settings
from rebe_agent.evolution import EvolutionClient
from rebe_agent.inbound import InboundMessage, parse
from rebe_agent.memory import InMemoryGroupMemory, Turn
from rebe_agent.pacer import Envelope, Pacer
from rebe_agent.pause import InMemoryPauseSwitch, Pause
from rebe_agent.reply import (
    MAX_REPLY_CHARS,
    THREAD_TURNS,
    Reply,
    ReplyLeg,
    ReplyRejectedError,
    Topic,
    render,
)
from rebe_agent.sends import InMemorySendLog, SendKind
from rebe_agent.usage import CallType, InMemoryUsageStore
from tests.deepseek_stub import FakeDeepSeek, tool_call_response
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import GROUP, MEXICO_CITY, NOON, RecordingAlerter
from tests.test_chimeins import Rolls
from tests.test_config import COMPLETE_ENV
from tests.webhooks import ANA, AT_EPOCH, BETO, edited, payload

SEED = 20260725

ROOMY = Envelope(post_gap=(timedelta(0), timedelta(0)))
"""The post-to-post gap lifted. It never bound a reply anyway; this keeps a test
that sends twice from arguing with a rule the news leg owns."""

VOICE = "jaja no creo, mas bien te lo hace mas facil"

CHIMED_IN = "pues a mi el nuevo se me hizo mas rapido que el anterior"

YES = 0.0
"""A draw below the chime-in probability: this eligible message becomes one."""

NO = 0.99
"""A draw above it, which is what most eligible messages get."""


def verdict(topic: Topic | str = Topic.ON_TOPIC, confidence: float = 0.9) -> dict[str, Any]:
    return tool_call_response(json.dumps({"topic": str(topic), "confidence": confidence}))


def about_ai(is_it: bool = True, confidence: float = 0.9) -> dict[str, Any]:
    """The tier-two gate's verdict on a message nobody addressed to her."""
    return tool_call_response(json.dumps({"about_ai": is_it, "confidence": confidence}))


def wrote(text: str = VOICE) -> dict[str, Any]:
    return tool_call_response(json.dumps({"text": text}))


def message(name: str = "by_name", **edits: Any) -> InboundMessage:
    parsed = parse(edited(name, **edits) if edits else payload(name))
    assert parsed is not None, f"{name} did not parse"
    return parsed


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
def memory() -> InMemoryGroupMemory:
    return InMemoryGroupMemory()


def make_brain(settings: Settings, fake: FakeDeepSeek, clock: ManualClock) -> Brain:
    return build_brain(
        settings, clock, InMemoryUsageStore(), RecordingAlerter(), http_client=fake.client()
    )


def make_leg(
    settings: Settings,
    fake: FakeDeepSeek,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
    *,
    log: InMemorySendLog | None = None,
    envelope: Envelope | None = None,
    brain: Brain | None = None,
    pause: Pause | None = None,
    budget: ChimeInBudget | None = None,
) -> ReplyLeg:
    """The real leg, against a fake DeepSeek and a fake Evolution.

    The chime-in budget defaults to one that never says yes, so a test about the
    addressed tier is never surprised by an unprompted message it did not ask for.
    """
    client = EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client())
    pacer = Pacer(
        client,
        log or InMemorySendLog(),
        clock,
        envelope=envelope or ROOMY,
        sleeper=ManualSleeper(clock),
        rng=random.Random(SEED),
        pause=pause,
    )
    return ReplyLeg(
        brain or make_brain(settings, fake, clock),
        pacer,
        client,
        memory,
        budget or make_budget(clock),
    )


def make_budget(
    clock: ManualClock,
    *draws: float,
    chime_ins: InMemoryChimeInLog | None = None,
    per_day: int = CHIME_INS_PER_DAY,
) -> ChimeInBudget:
    """A budget whose rolls a test names outright. No draws means she never does."""
    return ChimeInBudget(
        chime_ins or InMemoryChimeInLog(), clock, rng=Rolls(*draws), per_day=per_day
    )


# --- the reply the group sees -------------------------------------------------


def test_a_reply_is_one_short_line() -> None:
    assert render(Reply(text="  jaja  no creo   "), Topic.ON_TOPIC) == "jaja no creo"


def test_an_essay_is_not_a_whatsapp_reply() -> None:
    with pytest.raises(ReplyRejectedError, match="characters"):
        render(Reply(text="a" * (MAX_REPLY_CHARS + 1)), Topic.ON_TOPIC)


def test_an_empty_answer_is_not_an_answer() -> None:
    with pytest.raises(ReplyRejectedError, match="nothing"):
        render(Reply(text="   "), Topic.ON_TOPIC)


def test_a_reply_that_invents_a_link_is_refused() -> None:
    """She has been given no article to link to, so any address in a chat reply
    is one the model made up."""
    with pytest.raises(ReplyRejectedError, match="link"):
        render(Reply(text="mira en https://openai.com/x"), Topic.ON_TOPIC)


def test_a_reply_that_invents_a_figure_is_refused() -> None:
    """The anti-hallucination rule with nothing to ground it: unlike a news post
    there is no source item here, so a statistic or a date can only be invented."""
    with pytest.raises(ReplyRejectedError, match="figure"):
        render(Reply(text="salio en 2024 segun leí"), Topic.ON_TOPIC)


def test_a_small_number_is_still_ordinary_speech() -> None:
    assert render(Reply(text="ese es gpt 5, no el 4"), Topic.ON_TOPIC) == "ese es gpt 5, no el 4"


def test_more_than_one_emoji_is_refused() -> None:
    with pytest.raises(ReplyRejectedError, match="emoji"):
        render(Reply(text="jaja 😅🔥"), Topic.ON_TOPIC)


@pytest.mark.parametrize(
    "confession",
    [
        "si, soy un bot 🤖",
        "soy una IA entrenada por deepseek",
        "la verdad soy un asistente virtual",
        "no soy una persona, perdon",
    ],
)
def test_a_reply_that_confirms_she_is_a_bot_never_leaves_the_process(confession: str) -> None:
    """The persona and consent specs both say she never confirms. The prompt asks
    for that; this is what makes it true on the one time in fifty it is ignored."""
    with pytest.raises(ReplyRejectedError, match="character"):
        render(Reply(text=confession), Topic.ON_TOPIC)


@pytest.mark.parametrize("topic", [Topic.NO_GO, Topic.BOT_QUESTION])
def test_a_deflection_that_steers_back_to_ai_is_refused(topic: Topic) -> None:
    """ "She does not try to redirect the conversation back to AI - an
    agenda-driven redirect reads as a bot." On a no-go topic and on "¿eres un
    bot?", naming her own subject at all is that redirect."""
    with pytest.raises(ReplyRejectedError, match="steers back to AI"):
        render(Reply(text="uy ni idea, pero oye ya viste lo de la IA?"), topic)


def test_talking_about_ai_is_the_whole_point_everywhere_else() -> None:
    answer = "pues la IA de imagenes ya quedo bien chida"

    assert render(Reply(text=answer), Topic.ON_TOPIC) == answer


# --- one addressed message ----------------------------------------------------


async def test_an_addressed_message_gets_a_paced_reply_in_her_voice(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The headline acceptance criterion, and the order it happens in: she reads
    the message, appears to type, answers, and stops typing."""
    fake = FakeDeepSeek(verdict(), wrote())
    leg = make_leg(settings, fake, evolution, memory, clock)

    sent = await leg.handle(message("by_name"))

    assert sent is not None
    assert evolution.shape == ["read", "composing", "text", "paused"]
    assert evolution.texts == [VOICE]
    assert evolution.reads == ["3EB0A1B2C3D4E5F60002"]
    assert sent.typing_seconds > 0


async def test_a_paused_rebe_does_not_answer_even_when_addressed(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The switch has to reach this leg, not only the news one.

    The pacer's pause argument defaults to "never paused", so a reply leg wired
    without it would keep answering while an operator believed Rebe was silent -
    and it would keep answering *quietly*, with every other test still green.
    This is the test that fails if that wiring is ever dropped.
    """
    switch = InMemoryPauseSwitch(clock)
    await switch.set_paused(True, reason="cool it for a bit")
    fake = FakeDeepSeek(verdict(), wrote())
    leg = make_leg(settings, fake, evolution, memory, clock, pause=switch)

    assert await leg.handle(message("by_name")) is None
    assert evolution.texts == [], "a paused Rebe says nothing, addressed or not"
    assert "composing" not in evolution.shape, "nor does she look like she is about to"
    # Nor do the blue ticks land. A read receipt with no answer behind it is the
    # one thing worse than silence: it says she saw the message and chose not to
    # reply. A paused Rebe looks like a phone face down on a table.
    assert evolution.shape == [], "a pause is put the phone down, not read and ignore"
    assert evolution.reads == []
    assert fake.requests == [], "and a message she will not answer is not worth a token"


@pytest.mark.parametrize("name", ["mention", "by_name", "caption"])
async def test_every_form_of_a_name_tag_is_answered(
    name: str,
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    leg = make_leg(settings, FakeDeepSeek(verdict(), wrote()), evolution, memory, clock)

    assert await leg.handle(message(name)) is not None
    assert evolution.texts == [VOICE]


async def test_a_quote_of_one_of_her_own_messages_is_answered(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """She is answered without being named at all, which is how a WhatsApp reply
    to a message works."""
    await memory.remember(
        Turn(
            chat=GROUP,
            at=NOON - timedelta(minutes=90),
            message_id="STUB-MESSAGE-ID",
            author="",
            author_name="Rebe",
            text="miren salio un modelo que corre en tu compu",
            by_rebe=True,
        )
    )
    leg = make_leg(settings, FakeDeepSeek(verdict(), wrote()), evolution, memory, clock)

    assert await leg.handle(message("quote")) is not None


async def test_the_send_is_recorded_as_a_reply_not_as_a_post(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Which is what exempts it from the overnight hold the news leg obeys."""
    log = InMemorySendLog()
    leg = make_leg(settings, FakeDeepSeek(verdict(), wrote()), evolution, memory, clock, log=log)

    await leg.handle(message("by_name"))

    latest = await log.latest()
    assert latest is not None
    assert latest.kind is SendKind.REPLY
    assert latest.chat == GROUP


async def test_she_still_answers_at_midnight(
    settings: Settings, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> None:
    """The overnight hold is on scheduled posts. A name-tag at 23:30 gets an
    answer, because a person who is awake answers."""
    clock = ManualClock(NOON.replace(hour=23, minute=30))
    leg = make_leg(
        settings,
        FakeDeepSeek(verdict(), wrote()),
        evolution,
        memory,
        clock,
        envelope=Envelope(),
    )

    assert await leg.handle(message("by_name")) is not None
    assert evolution.texts == [VOICE]


async def test_both_deepseek_calls_land_where_the_budget_expects_them(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Call B is the gate, call C is the reply. Section 2 of the token budget spec
    counts them separately because only C ever becomes prose in the group."""
    store = InMemoryUsageStore()
    brain = build_brain(
        settings,
        clock,
        store,
        RecordingAlerter(),
        http_client=FakeDeepSeek(verdict(), wrote()).client(),
    )
    leg = make_leg(settings, FakeDeepSeek(), evolution, memory, clock, brain=brain)

    await leg.handle(message("by_name"))

    totals = await store.totals_on(clock.now().date())
    assert totals[CallType.REPLY_GATE].calls == 1
    assert totals[CallType.REPLY_GENERATION].calls == 1


# --- what she will not say ----------------------------------------------------


async def test_an_off_topic_address_is_deflected_rather_than_answered(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """ "Rebe where do you live" gets a human brush-off, not a lookup - so the
    instructions the model is given for an off-topic turn are not the on-topic
    ones."""
    fake = FakeDeepSeek(verdict(Topic.OFF_TOPIC), wrote("ando ocupada, luego les cuento"))
    leg = make_leg(settings, fake, evolution, memory, clock)

    await leg.handle(message("by_name", text="rebe donde vives?"))

    assert evolution.texts == ["ando ocupada, luego les cuento"]
    written = _instructions(fake.requests[-1])
    assert "buscador" in written, "the deflection instruction is the one that went out"


async def test_a_no_go_topic_gets_one_deflection_and_then_silence(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Deflect-and-drop, and no steering back to AI: the second push in the same
    thread gets nothing at all."""
    fake = FakeDeepSeek(
        verdict(Topic.NO_GO),
        wrote("uy de eso ni idea, mejor pregunta a alguien que sepa"),
        verdict(Topic.NO_GO),
        wrote("y por cierto, hablando de IA..."),
    )
    leg = make_leg(settings, fake, evolution, memory, clock)

    await leg.handle(message("by_name", text="rebe que medicina tomo pa la gripa?"))
    clock.advance(timedelta(minutes=1))
    second = await leg.handle(
        message(
            "by_name",
            text="ya en serio rebe, que me tomo?",
            message_id="3EB0SECONDPUSH",
            at_epoch=AT_EPOCH + 300,
        )
    )

    assert second is None
    assert evolution.texts == ["uy de eso ni idea, mejor pregunta a alguien que sepa"]


@pytest.mark.parametrize(
    "asked",
    [
        "rebe que medicina tomo pa la gripa?",
        "rebe puedo demandar a mi casero por esto?",
        "rebe le entro al bitcoin o no?",
        "rebe por quien vas a votar?",
        "rebe crees en dios?",
        "rebe me pasas el telefono de ana?",
        "rebe mandame una foto sexy",
    ],
)
async def test_every_no_go_category_gets_the_same_short_deflection(
    asked: str,
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Advice with real-world stakes, politics, religion, members' data, NSFW.
    The gate names the category; what this leg owes each of them is one line."""
    deflection = "uy de eso ni idea, mejor pregunta a alguien que sepa"
    fake = FakeDeepSeek(verdict(Topic.NO_GO), wrote(deflection))
    leg = make_leg(settings, fake, evolution, memory, clock)

    await leg.handle(message("by_name", text=asked))

    assert evolution.texts == [deflection]
    written = _instructions(fake.requests[-1])
    assert "no cambies la plática a IA" in written


async def test_a_bot_question_is_deflected_and_never_confirmed(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    fake = FakeDeepSeek(verdict(Topic.BOT_QUESTION), wrote("jajaja por? ya me dijeron eso antes"))
    leg = make_leg(settings, fake, evolution, memory, clock)

    await leg.handle(message("bot_question"))

    assert evolution.texts == ["jajaja por? ya me dijeron eso antes"]


async def test_a_confession_is_dropped_rather_than_sent(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """If the model breaks character, the group hears nothing. Silence is in
    character; "soy un bot" is the end of the persona."""
    fake = FakeDeepSeek(verdict(Topic.BOT_QUESTION), wrote("si, soy un bot"))
    leg = make_leg(settings, fake, evolution, memory, clock)

    assert await leg.handle(message("bot_question")) is None
    assert evolution.texts == []


# --- failure is silence -------------------------------------------------------


async def test_a_broken_deepseek_puts_nothing_at_all_in_the_group(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The acceptance criterion, and the failure posture behind it: a dropped
    reply looks like she put her phone down, and there is never an error message."""
    leg = make_leg(settings, FakeDeepSeek(500), evolution, memory, clock)

    assert await leg.handle(message("by_name")) is None
    assert evolution.texts == []


async def test_a_generation_that_fails_after_the_gate_is_also_silence(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    leg = make_leg(settings, FakeDeepSeek(verdict(), 500), evolution, memory, clock)

    assert await leg.handle(message("by_name")) is None
    assert evolution.texts == []
    assert evolution.reads == ["3EB0A1B2C3D4E5F60002"], "she read it, then said nothing"


async def test_a_low_confidence_classification_is_silence(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Ambiguity fails toward quiet, and this is the one thing that overrides
    "always answers when addressed"."""
    leg = make_leg(
        settings, FakeDeepSeek(verdict(confidence=0.2), wrote()), evolution, memory, clock
    )

    assert await leg.handle(message("by_name")) is None
    assert evolution.texts == []


async def test_a_refused_send_leaves_no_trace_in_the_group(
    settings: Settings, evolution: FakeEvolution, memory: InMemoryGroupMemory, clock: ManualClock
) -> None:
    """The envelope closing is not an error the group should ever hear about."""
    leg = make_leg(
        settings,
        _a_different_answer_each_time(4),
        evolution,
        memory,
        clock,
        envelope=Envelope(sends_per_hour=1),
    )
    await leg.handle(message("by_name"))
    clock.advance(timedelta(minutes=1))

    second = await leg.handle(
        message("by_name", text="rebe y que mas?", message_id="3EB0SECOND", at_epoch=AT_EPOCH + 120)
    )

    assert second is None
    assert len(evolution.texts) == 1


async def test_a_broken_transport_is_silence_too(
    settings: Settings, memory: InMemoryGroupMemory, clock: ManualClock
) -> None:
    broken = FakeEvolution()
    broken.text_status = 500
    leg = make_leg(settings, FakeDeepSeek(verdict(), wrote()), broken, memory, clock)

    assert await leg.handle(message("by_name")) is None


async def test_a_read_receipt_that_fails_does_not_cost_the_reply(
    settings: Settings, memory: InMemoryGroupMemory, clock: ManualClock
) -> None:
    """The receipt is camouflage; the reply is the point. Losing the first is not
    a reason to lose the second."""
    grumpy = FakeEvolution()
    grumpy.read_status = 500
    leg = make_leg(settings, FakeDeepSeek(verdict(), wrote()), grumpy, memory, clock)

    assert await leg.handle(message("by_name")) is not None
    assert grumpy.texts == [VOICE]


# --- the tier this leg never answers ------------------------------------------


@pytest.mark.parametrize("name", ["sticker", "from_rebe", "direct_message"])
async def test_the_silent_tier_gets_nothing_and_costs_nothing(
    name: str,
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Her own echo, a private chat, and a sticker with no words in it. None of
    them is a message a person would answer, and none of them reaches the gate:
    there is nothing there to classify."""
    fake = FakeDeepSeek(about_ai(), wrote())
    leg = make_leg(settings, fake, evolution, memory, clock, budget=make_budget(clock, YES))

    assert await leg.handle(message(name)) is None
    assert evolution.calls == []
    assert fake.requests == [], "a message with nothing readable in it costs no token"


async def test_unaddressed_chatter_is_still_remembered(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """She is quiet, not absent: the turn is in the window so the next reply has
    the thread behind it."""
    leg = make_leg(settings, FakeDeepSeek(about_ai(False)), evolution, memory, clock)

    await leg.handle(message("chatter"))

    assert [turn.text for turn in await memory.recent(GROUP)] == [
        "oigan ya probaron el modelo nuevo de openai?"
    ]


# --- tier two: the unprompted chime-in ----------------------------------------


async def test_an_unaddressed_ai_message_can_become_a_chime_in(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The headline acceptance criterion of this tier: nobody asked her, the
    group is talking about AI, and she joins in - paced like everything else."""
    fake = FakeDeepSeek(about_ai(), wrote(CHIMED_IN))
    leg = make_leg(settings, fake, evolution, memory, clock, budget=make_budget(clock, YES))

    sent = await leg.handle(message("chatter"))

    assert sent is not None
    assert evolution.texts == [CHIMED_IN]
    assert evolution.shape == ["read", "composing", "text", "paused"]
    assert sent.typing_seconds > 0


async def test_a_chime_in_goes_out_through_the_shared_pacer(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Not a second send path: it is written to the same log the news posts and
    the addressed replies are counted from."""
    log = InMemorySendLog()
    leg = make_leg(
        settings,
        FakeDeepSeek(about_ai(), wrote(CHIMED_IN)),
        evolution,
        memory,
        clock,
        log=log,
        budget=make_budget(clock, YES),
    )

    await leg.handle(message("chatter"))

    latest = await log.latest()
    assert latest is not None
    assert latest.kind is SendKind.REPLY
    assert latest.chat == GROUP


async def test_a_chime_in_is_written_as_one_of_hers(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    leg = make_leg(
        settings,
        FakeDeepSeek(about_ai(), wrote(CHIMED_IN)),
        evolution,
        memory,
        clock,
        budget=make_budget(clock, YES),
    )

    await leg.handle(message("chatter"))

    window = list(await memory.recent(GROUP))
    assert [turn.by_rebe for turn in window] == [False, True]
    assert window[-1].text == CHIMED_IN


@pytest.mark.parametrize("name", ["chatter", "small_talk"])
async def test_talk_that_is_not_about_ai_produces_no_message_at_all(
    name: str,
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Ordinary chatter, and tech talk the gate does not call AI. The scope is
    narrow on purpose: "not all of tech"."""
    fake = FakeDeepSeek(about_ai(False), wrote(CHIMED_IN))
    leg = make_leg(settings, fake, evolution, memory, clock, budget=make_budget(clock, YES))

    assert await leg.handle(message(name)) is None
    assert evolution.calls == [], "not even a read receipt on a message she skipped"


async def test_every_inbound_message_still_reaches_the_gate(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """No keyword pre-filter in front of the model. Football talk has none of the
    words a regex would look for, and it costs a gate call anyway - because the
    same regex would have swallowed "el modelo nuevo de Anthropic"."""
    fake = FakeDeepSeek(about_ai(False))
    leg = make_leg(settings, fake, evolution, memory, clock, budget=make_budget(clock, YES))

    await leg.handle(message("small_talk"))

    assert len(fake.requests) == 1, "the gate judged it, nothing cheaper did"
    assert "alguien va al partido el sabado?" in json.dumps(fake.requests[0])


async def test_most_eligible_messages_get_nothing(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The roll is what makes her selective rather than a lurker. The rate itself
    is asserted over two thousand draws in `tests/test_chimeins.py`; what this
    proves is that the leg is wired to it."""
    fake = FakeDeepSeek(about_ai(), wrote(CHIMED_IN))
    leg = make_leg(settings, fake, evolution, memory, clock, budget=make_budget(clock, NO))

    assert await leg.handle(message("chatter")) is None
    assert evolution.calls == []
    assert len(fake.requests) == 1, "a message she will not send is not worth a second call"


async def test_a_gate_that_is_unsure_leaves_the_conversation_alone(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    leg = make_leg(
        settings,
        FakeDeepSeek(about_ai(confidence=0.3), wrote(CHIMED_IN)),
        evolution,
        memory,
        clock,
        budget=make_budget(clock, YES),
    )

    assert await leg.handle(message("chatter")) is None
    assert evolution.texts == []


async def test_a_broken_gate_is_silence_here_too(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    leg = make_leg(
        settings, FakeDeepSeek(500), evolution, memory, clock, budget=make_budget(clock, YES)
    )

    assert await leg.handle(message("chatter")) is None
    assert evolution.texts == []


async def test_a_paused_rebe_does_not_chime_in_either(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The twin of the addressed-tier pause test, and for the same reason: the
    switch has to reach every tier, and a tier wired past it would go on talking
    quietly while an operator believed Rebe was silent."""
    switch = InMemoryPauseSwitch(clock)
    await switch.set_paused(True, reason="cool it for a bit")
    fake = FakeDeepSeek(about_ai(), wrote(CHIMED_IN))
    leg = make_leg(
        settings,
        fake,
        evolution,
        memory,
        clock,
        pause=switch,
        budget=make_budget(clock, YES),
    )

    assert await leg.handle(message("chatter")) is None
    assert evolution.shape == [], "a pause is put the phone down, not read and ignore"
    assert fake.requests == [], "and a message she will not answer is not worth a token"


async def test_the_daily_ceiling_stops_her_whatever_the_roll_says(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Every roll here says yes and the cooldown is stepped over, so the only
    thing left to stop the last one is the ceiling."""
    fake = _a_different_chime_in_each_time(CHIME_INS_PER_DAY + 1)
    leg = make_leg(
        settings,
        fake,
        evolution,
        memory,
        clock,
        budget=make_budget(clock, *[YES] * (CHIME_INS_PER_DAY + 1)),
    )

    for turn in range(CHIME_INS_PER_DAY + 1):
        clock.advance(COOLDOWN)
        await leg.handle(_another_ai_message(turn))

    assert len(evolution.texts) == CHIME_INS_PER_DAY


async def test_two_chime_ins_never_land_back_to_back(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The day is nowhere near full; they are simply too close together."""
    leg = make_leg(
        settings,
        _a_different_chime_in_each_time(2),
        evolution,
        memory,
        clock,
        budget=make_budget(clock, YES, YES),
    )

    await leg.handle(_another_ai_message(0))
    clock.advance(COOLDOWN - timedelta(minutes=5))
    second = await leg.handle(_another_ai_message(1))

    assert second is None
    assert len(evolution.texts) == 1


async def test_an_addressed_reply_still_goes_out_after_the_ceiling(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The two budgets are separate. A day full of chime-ins must never be the
    reason a name-tag goes unanswered - tier one has no cap at all."""
    chime_ins = InMemoryChimeInLog()
    fake = FakeDeepSeek(about_ai(), verdict(), wrote())
    leg = make_leg(
        settings,
        fake,
        evolution,
        memory,
        clock,
        budget=make_budget(clock, YES, chime_ins=chime_ins, per_day=0),
    )

    assert await leg.handle(message("chatter")) is None
    assert await leg.handle(message("by_name")) is not None
    assert evolution.texts == [VOICE]


async def test_the_days_count_outlives_the_leg_that_made_it(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """A restart is not a fresh allowance: the count is in the store, not in the
    process. This is the same store handed to a second leg built from scratch."""
    chime_ins = InMemoryChimeInLog()
    before = make_leg(
        settings,
        _a_different_chime_in_each_time(CHIME_INS_PER_DAY),
        evolution,
        memory,
        clock,
        budget=make_budget(clock, *[YES] * CHIME_INS_PER_DAY, chime_ins=chime_ins),
    )
    for turn in range(CHIME_INS_PER_DAY):
        clock.advance(COOLDOWN)
        await before.handle(_another_ai_message(turn))

    after_restart = make_leg(
        settings,
        _a_different_chime_in_each_time(1),
        evolution,
        memory,
        clock,
        budget=make_budget(clock, YES, chime_ins=chime_ins),
    )
    clock.advance(COOLDOWN)

    assert await after_restart.handle(_another_ai_message(9)) is None
    assert len(evolution.texts) == CHIME_INS_PER_DAY


async def test_the_day_that_is_full_is_the_local_one(
    settings: Settings, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> None:
    """Noon in Mexico City is already 18:00 UTC. A count kept in UTC would hand
    her a fresh allowance in the middle of the evening."""
    clock = ManualClock(NOON, MEXICO_CITY)
    chime_ins = InMemoryChimeInLog()
    leg = make_leg(
        settings,
        _a_different_chime_in_each_time(CHIME_INS_PER_DAY + 1),
        evolution,
        memory,
        clock,
        budget=make_budget(clock, *[YES] * (CHIME_INS_PER_DAY + 1), chime_ins=chime_ins),
        envelope=Envelope(
            sends_per_hour=99, sends_per_day=99, post_gap=(timedelta(0), timedelta(0))
        ),
    )
    for turn in range(CHIME_INS_PER_DAY):
        clock.advance(COOLDOWN)
        await leg.handle(_another_ai_message(turn))

    clock.advance(timedelta(hours=3))

    assert await leg.handle(_another_ai_message(8)) is None, "19:30 local is still today"
    assert len(evolution.texts) == CHIME_INS_PER_DAY


async def test_a_media_only_message_produces_silence(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """A photo with no caption. She does not guess at what a picture said."""
    fake = FakeDeepSeek(about_ai(), wrote(CHIMED_IN))
    leg = make_leg(settings, fake, evolution, memory, clock, budget=make_budget(clock, YES))

    assert await leg.handle(message("photo")) is None
    assert evolution.calls == []
    assert fake.requests == []


async def test_the_same_photo_with_a_caption_is_answered_on_the_caption(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """ "If readable text accompanies the media she responds to the text." """
    caption = "miren la grafica del modelo nuevo de openai"
    fake = FakeDeepSeek(about_ai(), wrote(CHIMED_IN))
    leg = make_leg(settings, fake, evolution, memory, clock, budget=make_budget(clock, YES))

    assert await leg.handle(message("photo", text=caption)) is not None
    assert evolution.texts == [CHIMED_IN]
    assert caption in json.dumps(fake.requests[0]), "the caption is what the gate judged"


async def test_an_english_message_is_answered_in_spanish(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """She understands English and answers in her own voice anyway: switching
    language on command is a tell, and the instruction that says so is what the
    model is actually handed."""
    fake = FakeDeepSeek(about_ai(), wrote(CHIMED_IN))
    leg = make_leg(settings, fake, evolution, memory, clock, budget=make_budget(clock, YES))

    await leg.handle(message("chatter", text="anyone tried the new openai model yet?"))

    assert evolution.texts == [CHIMED_IN]
    written = _instructions(fake.requests[-1])
    assert "Siempre en español, aunque te escriban en inglés" in written


async def test_a_chime_in_is_told_that_nobody_asked_her(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The generation call is given the situation, not only the topic. A model
    told "answer this" writes an answer, and an answer to a question nobody asked
    is the shape a helpdesk has."""
    fake = FakeDeepSeek(about_ai(), wrote(CHIMED_IN))
    leg = make_leg(settings, fake, evolution, memory, clock, budget=make_budget(clock, YES))

    await leg.handle(message("chatter"))

    written = _instructions(fake.requests[-1])
    assert "Nadie te habló a ti" in written
    assert "Alguien te habló directo" not in written


def _another_ai_message(turn: int) -> InboundMessage:
    """A fresh unaddressed message about AI, far enough from the last to be its
    own thread rather than a continuation of one she has already spoken in."""
    return message(
        "chatter",
        text=f"y ya vieron el otro modelo, el numero {turn}",
        message_id=f"3EB0CHIME{turn}",
        at_epoch=AT_EPOCH + 7_200 * (turn + 1),
    )


def _a_different_chime_in_each_time(turns: int) -> FakeDeepSeek:
    """An eligibility verdict and a *distinct* chime-in for each of `turns` events.

    Distinct because the pacer refuses identical wording twice in a row, and a
    test about the day's ceiling should not trip over a rule the envelope owns.
    """
    return FakeDeepSeek(
        *(
            response
            for turn in range(turns)
            for response in (about_ai(), wrote(f"a mi ese se me hizo mejor, el {turn}"))
        )
    )


# --- conversation shape -------------------------------------------------------


async def test_a_redelivered_webhook_does_not_answer_twice(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Evolution retries a delivery it believes failed."""
    fake = FakeDeepSeek(verdict(), wrote(), verdict(), wrote())
    leg = make_leg(settings, fake, evolution, memory, clock)

    first = await leg.handle(message("by_name"))
    clock.advance(timedelta(minutes=1))
    again = await leg.handle(message("by_name"))

    assert first is not None
    assert again is None
    assert len(evolution.texts) == 1


async def test_she_does_not_answer_the_same_person_twice_running(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Two messages arrive together and she answers the first. The second was
    written before her answer, so answering it too would be two of hers in a row."""
    fake = FakeDeepSeek(verdict(), wrote(), verdict(), wrote())
    leg = make_leg(settings, fake, evolution, memory, clock)

    await leg.handle(message("by_name"))
    second = await leg.handle(
        message("by_name", text="rebe y tambien esto", message_id="3EB0SAMEBREATH")
    )

    assert second is None
    assert len(evolution.texts) == 1


async def test_somebody_else_speaking_up_is_not_twice_in_a_row(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The rule is about the same person, not about her."""
    fake = FakeDeepSeek(verdict(), wrote(), verdict(), wrote("va, yo digo que si"))
    leg = make_leg(settings, fake, evolution, memory, clock)

    await leg.handle(message("by_name"))
    second = await leg.handle(
        message("by_name", text="rebe tu que dices", author=BETO, message_id="3EB0FROMBETO")
    )

    assert second is not None
    assert len(evolution.texts) == 2


async def test_a_thread_fades_after_a_few_turns(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """She answers a follow-up or two and then lets it die - with no closing
    message, which is what a human putting the phone down looks like."""
    leg = make_leg(settings, _a_different_answer_each_time(6), evolution, memory, clock)

    for turn in range(THREAD_TURNS + 2):
        clock.advance(timedelta(minutes=1))
        await leg.handle(
            message(
                "by_name",
                text=f"rebe y que tal lo otro numero {turn}",
                message_id=f"3EB0TURN{turn}",
                at_epoch=AT_EPOCH + 120 * (turn + 1),
            )
        )

    assert len(evolution.texts) == THREAD_TURNS
    assert not any("adios" in text.lower() for text in evolution.texts)


async def test_a_thread_that_went_quiet_starts_again(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The fade is per thread, not per group: an hour later is a new conversation."""
    leg = make_leg(settings, _a_different_answer_each_time(8), evolution, memory, clock)
    for turn in range(THREAD_TURNS):
        clock.advance(timedelta(minutes=1))
        await leg.handle(
            message(
                "by_name",
                text=f"rebe cuentame lo numero {turn}",
                message_id=f"3EB0EARLY{turn}",
                at_epoch=AT_EPOCH + 120 * (turn + 1),
            )
        )

    clock.advance(timedelta(hours=3))
    later = await leg.handle(
        message(
            "by_name",
            text="rebe ya volvi, que hubo",
            message_id="3EB0LATER",
            at_epoch=AT_EPOCH + 10_800,
        )
    )

    assert later is not None
    assert len(evolution.texts) == THREAD_TURNS + 1


async def test_the_thread_so_far_is_handed_back_to_the_model(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """The rolling window is not decoration: the generation call carries the turns
    before it, which is what makes a follow-up land as a follow-up."""
    await memory.remember(
        Turn(
            chat=GROUP,
            at=NOON - timedelta(minutes=2),
            message_id="3EB0EARLIER",
            author=ANA,
            author_name="Ana",
            text="ya vieron el modelo que corre local?",
        )
    )
    fake = FakeDeepSeek(verdict(), wrote())
    leg = make_leg(settings, fake, evolution, memory, clock)

    await leg.handle(message("by_name"))

    written = json.dumps(fake.requests[-1])
    assert "ya vieron el modelo que corre local?" in written


async def test_her_own_answer_joins_the_window(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    leg = make_leg(settings, FakeDeepSeek(verdict(), wrote()), evolution, memory, clock)

    await leg.handle(message("by_name"))

    window = list(await memory.recent(GROUP))
    assert [turn.by_rebe for turn in window] == [False, True]
    assert window[-1].text == VOICE
    assert window[-1].reply_to == ANA
    assert window[-1].message_id == "STUB-MESSAGE-ID"


async def test_her_own_news_post_joins_the_window_as_hers(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Evolution echoes her news posts back through the same webhook. Filed as
    somebody else's line they would read as a stranger talking in the thread she
    is handed, and their ids would never make a quote of them tier one."""
    leg = make_leg(settings, FakeDeepSeek(verdict(), wrote()), evolution, memory, clock)

    assert await leg.handle(message("from_rebe")) is None

    window = list(await memory.recent(GROUP))
    assert [turn.by_rebe for turn in window] == [True]
    assert evolution.calls == []


async def test_two_deliveries_at_once_are_still_one_reply(
    settings: Settings,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    clock: ManualClock,
) -> None:
    """Both arrive before either has been answered. Run concurrently they would
    each read a window in which she had not spoken yet, and both would send."""
    leg = make_leg(settings, _a_different_answer_each_time(4), evolution, memory, clock)

    await asyncio.gather(
        leg.handle(message("by_name")),
        leg.handle(message("by_name", text="rebe y tambien esto", message_id="3EB0ATONCE")),
    )

    assert len(evolution.texts) == 1


def _a_different_answer_each_time(turns: int) -> FakeDeepSeek:
    """A gate verdict and a *distinct* reply for each of `turns` events.

    Distinct because the pacer refuses identical wording twice in a row, and a
    test about the shape of a conversation should not trip over that rule.
    """
    return FakeDeepSeek(
        *(
            response
            for turn in range(turns)
            for response in (verdict(), wrote(f"pues fijate que si, la {turn}"))
        )
    )


def _instructions(request: dict[str, Any]) -> str:
    """The system prompt of one recorded DeepSeek request."""
    return "\n".join(
        str(entry.get("content", ""))
        for entry in request.get("messages", [])
        if entry.get("role") == "system"
    )
