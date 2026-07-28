"""The shared pacer: what the group sees, and what the envelope refuses.

Nothing here waits on real time. The clock is moved by hand and the sleeping goes
through a `ManualSleeper` that advances that clock instead of the world, so a
test can sit at 03:00, spend ninety minutes, or fill an hour's ceiling in the
time it takes to run an assertion. The randomness is seeded, so "jittered" is
still repeatable.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import pytest

from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.evolution import COMPOSING, PAUSED, EvolutionClient, EvolutionError
from rebe_agent.pacer import (
    Envelope,
    Pacer,
    RefusalReason,
    SendRefusedError,
    TypingProfile,
)
from rebe_agent.sends import InMemorySendLog, SendKind, SendRecord, fingerprint
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import GROUP, MEXICO_CITY, NOON

SEED = 20260725

MESSAGE = "Nuevo modelo de OpenAI, ahora corre local. Se ve interesante."
"""Sixty-one characters: about 1830 ms of typing, comfortably inside the clamp."""

IMAGE = "https://openai.com/og/local-model.png"
CAPTION = MESSAGE + "\nhttps://openai.com/index/local-model"
"""A news photo: her words, a newline, the link - the same shape a text post has."""


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOON)


@pytest.fixture
def sleeper(clock: ManualClock) -> ManualSleeper:
    return ManualSleeper(clock)


@pytest.fixture
def log() -> InMemorySendLog:
    return InMemorySendLog()


@pytest.fixture
def evolution() -> FakeEvolution:
    return FakeEvolution()


def make_pacer(
    evolution: FakeEvolution,
    log: InMemorySendLog,
    clock: ManualClock,
    sleeper: ManualSleeper,
    *,
    envelope: Envelope | None = None,
    typing: TypingProfile | None = None,
    seed: int = SEED,
) -> Pacer:
    client = EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client())
    return Pacer(
        client,
        log,
        clock,
        envelope=envelope,
        typing=typing,
        sleeper=sleeper,
        rng=random.Random(seed),
    )


@pytest.fixture
def pacer(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> Pacer:
    return make_pacer(evolution, log, clock, sleeper)


ROOMY = Envelope(sends_per_hour=1000, sends_per_day=1000, post_gap=(timedelta(0), timedelta(0)))
"""The ceilings lifted, so a test can exercise exactly one rule at a time."""


async def seed_send(
    log: InMemorySendLog,
    at: datetime,
    *,
    kind: SendKind = SendKind.POST,
    text: str = "algo",
    day: date | None = None,
) -> None:
    """Place a send in the log, as a restart would find it.

    `day` defaults to the local day of `at`, which is what the pacer writes; a
    test that cares about the day boundary passes it explicitly.
    """
    await log.record(
        SendRecord(
            sent_at=at,
            day=day or at.astimezone(MEXICO_CITY).date(),
            kind=kind,
            chat=GROUP,
            fingerprint=fingerprint(text),
        )
    )


# --- Looking human -----------------------------------------------------------


async def test_the_group_sees_typing_before_the_message_arrives(
    pacer: Pacer, evolution: FakeEvolution
) -> None:
    await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert evolution.shape[0] == COMPOSING
    assert evolution.shape[-2:] == ["text", PAUSED]
    assert evolution.texts == [MESSAGE]


async def test_a_photo_goes_out_through_the_same_envelope_as_a_text(
    pacer: Pacer, evolution: FakeEvolution
) -> None:
    """Same composing presence, same cleared presence after; only the message
    itself is a photo with the caption instead of a bare text."""
    await pacer.send_photo(SendKind.POST, GROUP, IMAGE, CAPTION)

    assert evolution.shape[0] == COMPOSING
    assert evolution.shape[-2:] == ["media", PAUSED]
    assert evolution.medias == [
        {"number": GROUP, "mediatype": "image", "media": IMAGE, "caption": CAPTION}
    ]


async def test_every_call_carries_the_api_key_and_the_configured_instance(
    pacer: Pacer, evolution: FakeEvolution
) -> None:
    await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert {call.api_key for call in evolution.calls} == {API_KEY}
    assert all(call.path.endswith(f"/{INSTANCE}") for call in evolution.calls)
    assert all(call.body["number"] == GROUP for call in evolution.calls)


async def test_the_typing_delay_scales_with_the_length_of_the_message(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    short = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)
    brief = await short.send(SendKind.POST, GROUP, "a" * 60)
    long = await short.send(SendKind.POST, GROUP, "b" * 140)

    assert long.typing_seconds > brief.typing_seconds


async def test_the_typing_delay_never_leaves_the_clamp_and_never_repeats(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """Both halves of "Gaussian jitter, clamped 1500-5000 ms, never a constant"."""
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)

    sends = [
        await pacer.send(SendKind.POST, GROUP, f"mensaje numero {index} " + "x" * 40)
        for index in range(40)
    ]
    drawn = [sent.typing_seconds for sent in sends]

    assert all(1.5 <= seconds <= 5.0 for seconds in drawn)
    assert len(set(drawn)) == len(drawn)


async def test_a_message_shorter_than_the_floor_still_varies(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """Clipping short messages onto exactly 1500 ms would be the constant delay
    the playbook warns about, so the draw is folded back into the band instead."""
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)

    drawn = {
        (await pacer.send(SendKind.POST, GROUP, f"ok {index}")).typing_seconds
        for index in range(10)
    }

    assert len(drawn) == 10
    assert all(1.5 <= seconds <= 5.0 for seconds in drawn)


async def test_the_drawn_pause_is_what_evolution_is_asked_to_hold(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """The pause is drawn here and executed there, so the number crossing the wire
    is the whole of the contract: `delay` is in milliseconds, and it is the draw."""
    steady = TypingProfile(minimum_ms=4000, maximum_ms=4000)
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY, typing=steady)

    sent = await pacer.send(SendKind.POST, GROUP, MESSAGE)

    composing = [call for call in evolution.calls if call.presence == COMPOSING]
    assert len(composing) == 1
    assert composing[0].body["delay"] == round(sent.typing_seconds * 1000) == 4000
    assert evolution.shape == [COMPOSING, "text", PAUSED]


async def test_a_short_pause_needs_no_refresh(pacer: Pacer, evolution: FakeEvolution) -> None:
    await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert evolution.shape == [COMPOSING, "text", PAUSED]


async def test_the_first_message_into_a_quiet_thread_gets_an_extra_beat(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)

    opening = await pacer.send(SendKind.REPLY, GROUP, "primera cosa del dia")
    following = await pacer.send(SendKind.REPLY, GROUP, "y otra cosa mas")

    assert opening.waited_seconds > 1.0
    assert following.waited_seconds == 0.0


async def test_an_addressed_reply_still_lands_at_three_in_the_morning(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """02:00-06:00 is "near-silent", not silent. The reply policy still answers a
    human who tagged her by name; the hush governs how often, not whether."""
    clock.set(datetime(2026, 7, 25, 3, 0, tzinfo=MEXICO_CITY))
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)

    await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert evolution.texts == [MESSAGE]


async def test_the_small_hours_space_sends_four_to_six_times_further_apart(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """ "Slow 4-6x" needs a 1x to be four to six times slower than.

    The shipped envelope is the one thing these hush tests must not lift: the
    spacing the hush multiplies is the hourly ceiling spread out, so three an
    hour means one send every twenty minutes by day, and eighty to a hundred and
    twenty minutes at three in the morning.
    """
    clock.set(datetime(2026, 7, 25, 3, 0, tzinfo=MEXICO_CITY))
    pacer = make_pacer(evolution, log, clock, sleeper)
    await seed_send(log, clock.now() - timedelta(minutes=25), kind=SendKind.REPLY, text="antes")

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert refused.value.reason is RefusalReason.NIGHT_HUSH
    assert refused.value.retry_after is not None
    assert evolution.calls == []

    clock.advance(timedelta(minutes=125))  # 150 minutes on: past even a 6x gap
    await pacer.send(SendKind.REPLY, GROUP, MESSAGE)
    assert evolution.texts == [MESSAGE]


async def test_the_same_gap_in_the_afternoon_is_perfectly_normal(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """The control for the test above: same envelope, same gap, different hour."""
    pacer = make_pacer(evolution, log, clock, sleeper)
    await seed_send(log, clock.now() - timedelta(minutes=25), kind=SendKind.REPLY, text="antes")

    daytime = await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert evolution.texts == [MESSAGE]
    assert daytime.waited_seconds == 0.0


async def test_the_hush_holds_its_answer_still_when_a_caller_keeps_asking(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """Same reason as the post gap: a threshold redrawn per attempt is no threshold."""
    clock.set(datetime(2026, 7, 25, 3, 0, tzinfo=MEXICO_CITY))
    pacer = make_pacer(evolution, log, clock, sleeper)
    await seed_send(log, clock.now() - timedelta(minutes=25), kind=SendKind.REPLY, text="antes")

    waits = []
    for _ in range(20):
        with pytest.raises(SendRefusedError) as refused:
            await pacer.send(SendKind.REPLY, GROUP, MESSAGE)
        assert refused.value.reason is RefusalReason.NIGHT_HUSH
        waits.append(refused.value.retry_after)

    assert len(set(waits)) == 1


# --- The ceilings ------------------------------------------------------------


async def test_a_fifth_send_inside_a_minute_is_paced_rather_than_fired(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """The per-minute floor is a wait, not a refusal - a full minute window is
    empty again within a minute.

    The hourly ceiling is lifted here because with the shipped three-an-hour it
    binds first and the minute floor is unreachable. The floor is still the last
    burst guard if a ramp or a later spec raises that ceiling.
    """
    start = clock.now()
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)
    for index in range(4):
        await seed_send(
            log, start - timedelta(seconds=50 - index), kind=SendKind.REPLY, text=f"m{index}"
        )

    fifth = await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert evolution.texts == [MESSAGE]  # it does go out
    assert fifth.at >= start + timedelta(seconds=10)  # but only once the oldest aged out


async def test_four_in_a_minute_are_not_held_back(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    start = clock.now()
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)
    for index in range(3):
        await seed_send(
            log, start - timedelta(seconds=30 + index), kind=SendKind.REPLY, text=f"m{index}"
        )

    fourth = await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert fourth.waited_seconds == 0.0


async def test_the_hourly_ceiling_counts_posts_and_replies_together(
    pacer: Pacer, log: InMemorySendLog, clock: ManualClock
) -> None:
    """Two limiters, one per leg, would each stay under three and together send six."""
    await seed_send(log, clock.now() - timedelta(minutes=40), kind=SendKind.POST, text="uno")
    await seed_send(log, clock.now() - timedelta(minutes=30), kind=SendKind.REPLY, text="dos")
    await seed_send(log, clock.now() - timedelta(minutes=20), kind=SendKind.REPLY, text="tres")

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert refused.value.reason is RefusalReason.HOURLY_CEILING
    assert refused.value.retry_after is not None


async def test_a_photo_fills_the_same_ceiling_it_is_refused_by(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """Both directions: a full hour refuses a photo, and a photo that went out
    counts against the text that follows - the ceilings know one send log.

    The hour is filled with replies so the post-to-post gap, which would refuse
    a second post long before the ceiling matters, stays out of the way.
    """
    pacer = make_pacer(evolution, log, clock, sleeper)
    await seed_send(log, clock.now() - timedelta(minutes=40), kind=SendKind.REPLY, text="uno")
    await seed_send(log, clock.now() - timedelta(minutes=30), kind=SendKind.REPLY, text="dos")
    await seed_send(log, clock.now() - timedelta(minutes=20), kind=SendKind.REPLY, text="tres")

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send_photo(SendKind.POST, GROUP, IMAGE, CAPTION)
    assert refused.value.reason is RefusalReason.HOURLY_CEILING

    clock.advance(timedelta(minutes=45))  # the seeded hour has drained
    await pacer.send_photo(SendKind.POST, GROUP, IMAGE, CAPTION)
    await pacer.send(SendKind.REPLY, GROUP, "otra cosa distinta")
    await pacer.send(SendKind.REPLY, GROUP, "y otra mas")

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.REPLY, GROUP, MESSAGE)
    assert refused.value.reason is RefusalReason.HOURLY_CEILING


async def test_the_hour_is_a_rolling_window_not_a_clock_hour(
    pacer: Pacer, log: InMemorySendLog, clock: ManualClock, evolution: FakeEvolution
) -> None:
    await seed_send(log, clock.now() - timedelta(minutes=59), kind=SendKind.REPLY, text="uno")
    await seed_send(log, clock.now() - timedelta(minutes=58), kind=SendKind.REPLY, text="dos")
    await seed_send(log, clock.now() - timedelta(minutes=57), kind=SendKind.REPLY, text="tres")

    clock.advance(timedelta(minutes=4))
    await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert evolution.texts == [MESSAGE]


async def test_the_daily_ceiling_counts_posts_and_replies_together(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    pacer = make_pacer(
        evolution, log, clock, sleeper, envelope=Envelope(sends_per_hour=1000, sends_per_day=12)
    )
    for index in range(12):
        kind = SendKind.POST if index % 2 else SendKind.REPLY
        await seed_send(
            log, clock.now() - timedelta(hours=6, minutes=index), kind=kind, text=f"m{index}"
        )

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert refused.value.reason is RefusalReason.DAILY_CEILING


async def test_the_day_the_ceiling_counts_is_the_local_day_not_the_utc_one(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """At 23:30 in Mexico City it is already the next morning in UTC.

    Two sends earlier the same local evening have to fill the day. Reading the
    day in UTC would file them under yesterday and hand the evening a second
    allowance - which is the burst the ceiling exists to stop. The `day` on each
    seeded send is written the way the pacer writes it, so the only thing under
    test is which date the pacer asks the log about.
    """
    evening = datetime(2026, 7, 25, 23, 30, tzinfo=MEXICO_CITY)
    assert evening.astimezone(UTC).date() != evening.date(), "the two zones must disagree here"

    clock.set(evening)
    pacer = make_pacer(
        evolution, log, clock, sleeper, envelope=Envelope(sends_per_hour=1000, sends_per_day=2)
    )
    for index, minutes_ago in enumerate((180, 120)):
        await seed_send(
            log, evening - timedelta(minutes=minutes_ago), kind=SendKind.REPLY, text=f"m{index}"
        )

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert refused.value.reason is RefusalReason.DAILY_CEILING
    assert evolution.calls == []


async def test_yesterdays_sends_do_not_fill_todays_allowance(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """The other side of the same boundary: the count rolls at local midnight."""
    pacer = make_pacer(
        evolution, log, clock, sleeper, envelope=Envelope(sends_per_hour=1000, sends_per_day=2)
    )
    for index, hour in enumerate((22, 23)):
        await seed_send(
            log,
            datetime(2026, 7, 24, hour, 30, tzinfo=MEXICO_CITY),
            kind=SendKind.REPLY,
            text=f"m{index}",
        )

    await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert evolution.texts == [MESSAGE]


# --- Quiet hours -------------------------------------------------------------


@pytest.mark.parametrize("hour", [23, 2, 3, 6, 7])
async def test_a_scheduled_post_overnight_is_refused(
    hour: int,
    evolution: FakeEvolution,
    log: InMemorySendLog,
    clock: ManualClock,
    sleeper: ManualSleeper,
) -> None:
    clock.set(datetime(2026, 7, 25, hour, 15, tzinfo=MEXICO_CITY))
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert refused.value.reason is RefusalReason.OVERNIGHT_HOLD
    assert refused.value.retry_after is not None
    assert evolution.calls == []


@pytest.mark.parametrize("hour", [8, 12, 22])
async def test_a_scheduled_post_inside_waking_hours_is_not_held(
    hour: int,
    evolution: FakeEvolution,
    log: InMemorySendLog,
    clock: ManualClock,
    sleeper: ManualSleeper,
) -> None:
    clock.set(datetime(2026, 7, 25, hour, 15, tzinfo=MEXICO_CITY))
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)

    await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert evolution.texts == [MESSAGE]


async def test_at_three_in_the_morning_a_post_is_refused_while_a_reply_goes_out(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """The two legs part company exactly here: the cadence spec holds every
    scheduled post overnight, and the reply policy still answers a human who
    tagged her by name."""
    clock.set(datetime(2026, 7, 25, 3, 0, tzinfo=MEXICO_CITY))
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.POST, GROUP, "una noticia que puede esperar")
    assert refused.value.reason is RefusalReason.OVERNIGHT_HOLD

    await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert evolution.texts == [MESSAGE]


async def test_a_photo_post_is_held_overnight_exactly_as_a_text_post_is(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """A picture changes nothing about when Rebe sleeps."""
    clock.set(datetime(2026, 7, 25, 3, 0, tzinfo=MEXICO_CITY))
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send_photo(SendKind.POST, GROUP, IMAGE, CAPTION)

    assert refused.value.reason is RefusalReason.OVERNIGHT_HOLD
    assert evolution.calls == []


async def test_consecutive_posts_are_spaced_by_at_least_seventy_five_minutes(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=Envelope(sends_per_hour=1000))
    await seed_send(log, clock.now() - timedelta(minutes=70), kind=SendKind.POST, text="anterior")

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert refused.value.reason is RefusalReason.MINIMUM_GAP

    clock.advance(timedelta(minutes=25))  # 95 minutes since the last post
    await pacer.send(SendKind.POST, GROUP, MESSAGE)
    assert evolution.texts == [MESSAGE]


async def test_the_post_gap_gives_the_same_answer_however_often_it_is_asked(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """A gap redrawn per attempt is not a gap.

    The cadence spec asks for 75-90 minutes, jittered. Drawn fresh each time it
    is asked, a caller that retries every minute keeps rolling until it gets the
    shortest gap on offer, so the spread quietly collapses to a flat 75 and the
    periodic rhythm the jitter existed to break is back. The threshold is read
    off the previous post instead, so asking twenty times gives one answer.
    """
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=Envelope(sends_per_hour=1000))
    await seed_send(log, clock.now() - timedelta(minutes=76), kind=SendKind.POST, text="anterior")

    waits = []
    for _ in range(20):
        with pytest.raises(SendRefusedError) as refused:
            await pacer.send(SendKind.POST, GROUP, MESSAGE)
        assert refused.value.reason is RefusalReason.MINIMUM_GAP
        waits.append(refused.value.retry_after)

    assert len(set(waits)) == 1


async def test_the_post_gap_is_not_the_same_for_every_post(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """Stable per post, but still spread across posts - or it is just a constant."""
    elapsed = timedelta(minutes=74)
    required = set()

    for index in range(25):
        one_post = InMemorySendLog()
        pacer = make_pacer(
            evolution, one_post, clock, sleeper, envelope=Envelope(sends_per_hour=1000)
        )
        await seed_send(
            one_post, clock.now() - elapsed, kind=SendKind.POST, text=f"noticia {index}"
        )

        with pytest.raises(SendRefusedError) as refused:
            await pacer.send(SendKind.POST, GROUP, MESSAGE)

        assert refused.value.retry_after is not None
        required.add(refused.value.retry_after + elapsed)

    assert len(required) > 20, "the gap is barely jittered at all"
    assert all(timedelta(minutes=75) <= gap <= timedelta(minutes=90) for gap in required)


async def test_a_reply_is_not_held_by_the_post_gap(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=Envelope(sends_per_hour=1000))
    await seed_send(log, clock.now() - timedelta(minutes=2), kind=SendKind.POST, text="anterior")

    await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert evolution.texts == [MESSAGE]


# --- Repeats, restarts and refusals ------------------------------------------


async def test_the_same_wording_twice_in_a_row_is_refused(
    pacer: Pacer, evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock
) -> None:
    await seed_send(log, clock.now() - timedelta(minutes=20), kind=SendKind.REPLY, text=MESSAGE)

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert refused.value.reason is RefusalReason.DUPLICATE
    assert evolution.calls == []


async def test_spacing_and_case_do_not_make_it_a_different_message(
    pacer: Pacer, log: InMemorySendLog, clock: ManualClock
) -> None:
    await seed_send(
        log, clock.now() - timedelta(minutes=20), kind=SendKind.REPLY, text="Hola   a todos"
    )

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.REPLY, GROUP, "hola a todos")

    assert refused.value.reason is RefusalReason.DUPLICATE


async def test_the_same_wording_is_fine_once_something_else_has_gone_out(
    pacer: Pacer, evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock
) -> None:
    """ "Never twice in a row" is about consecutive messages, not about a ban list."""
    await seed_send(log, clock.now() - timedelta(minutes=40), kind=SendKind.REPLY, text=MESSAGE)
    await seed_send(log, clock.now() - timedelta(minutes=20), kind=SendKind.REPLY, text="otra cosa")

    await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert evolution.texts == [MESSAGE]


async def test_a_restart_keeps_the_days_counts(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """The log is the state, not the object: a second pacer over the same log is
    what a crash loop looks like, and it must not get a fresh allowance."""
    envelope = Envelope(sends_per_hour=1000, sends_per_day=3)
    before = make_pacer(evolution, log, clock, sleeper, envelope=envelope)
    for index in range(3):
        await before.send(SendKind.REPLY, GROUP, f"mensaje {index}")
        clock.advance(timedelta(minutes=5))

    after_restart = make_pacer(evolution, log, clock, sleeper, envelope=envelope)

    with pytest.raises(SendRefusedError) as refused:
        await after_restart.send(SendKind.REPLY, GROUP, MESSAGE)
    assert refused.value.reason is RefusalReason.DAILY_CEILING


async def test_a_refusal_is_not_a_transport_error(
    pacer: Pacer, log: InMemorySendLog, clock: ManualClock
) -> None:
    """The whole point of the two types: "come back later" must not read as "broken"."""
    await seed_send(log, clock.now() - timedelta(minutes=20), kind=SendKind.REPLY, text=MESSAGE)

    with pytest.raises(SendRefusedError) as refused:
        await pacer.send(SendKind.REPLY, GROUP, MESSAGE)

    assert not isinstance(refused.value, EvolutionError)
    assert refused.value.reason.value in str(refused.value)


async def test_a_transport_failure_is_not_a_refusal(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    evolution.text_status = 500
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)

    with pytest.raises(EvolutionError) as failed:
        await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert not isinstance(failed.value, SendRefusedError)
    assert failed.value.status == 500


async def test_a_send_that_failed_in_transport_still_counts(
    evolution: FakeEvolution, log: InMemorySendLog, clock: ManualClock, sleeper: ManualSleeper
) -> None:
    """Otherwise a failing Evolution and a caller that retries is a burst - which
    is what the ceiling is for. Backing off is the playbook's answer, not retrying."""
    evolution.text_status = 500
    pacer = make_pacer(evolution, log, clock, sleeper, envelope=ROOMY)

    with pytest.raises(EvolutionError):
        await pacer.send(SendKind.POST, GROUP, MESSAGE)

    assert await log.count_on(clock.now().date()) == 1


async def test_an_empty_message_is_a_programming_error(pacer: Pacer) -> None:
    with pytest.raises(ValueError, match="empty"):
        await pacer.send(SendKind.POST, GROUP, "   ")
