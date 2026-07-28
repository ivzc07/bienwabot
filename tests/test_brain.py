"""What the brain puts on the wire, and what it refuses to hand back."""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel

from rebe_agent import news
from rebe_agent.brain import (
    CALL_SHAPES,
    DEEPSEEK_MODEL,
    Brain,
    BrainCallError,
    BrainStoppedError,
    build_brain,
)
from rebe_agent.clock import ManualClock
from rebe_agent.config import Settings, load_settings
from rebe_agent.guard import STOP_THRESHOLD
from rebe_agent.signals import Signal
from rebe_agent.usage import CallType, DayTotals, InMemoryUsageStore
from tests.deepseek_stub import FakeDeepSeek, json_output_response
from tests.support import NOON, TODAY, RecordingAlerter
from tests.test_config import COMPLETE_ENV


class NewsPost(BaseModel):
    """Stands in for the real news schema; any typed output exercises the path."""

    framing: str
    line: str
    url: str


VALID_POST = '{"framing": "Ojo", "line": "Sale un modelo nuevo.", "url": "https://x.mx/a"}'


@pytest.fixture
def settings() -> Settings:
    return load_settings(dict(COMPLETE_ENV))


@pytest.fixture
def store() -> InMemoryUsageStore:
    return InMemoryUsageStore()


def brain_for(
    settings: Settings,
    fake: FakeDeepSeek,
    store: InMemoryUsageStore,
    alerter: RecordingAlerter | None = None,
) -> Brain:
    return build_brain(
        settings,
        ManualClock(NOON),
        store,
        alerter or RecordingAlerter(),
        http_client=fake.client(),
    )


async def test_a_call_comes_back_as_a_validated_object(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    fake = FakeDeepSeek(json_output_response(VALID_POST))

    post = await brain_for(settings, fake, store).ask(
        CallType.NEWS_SUMMARY, "resume esto", NewsPost
    )

    assert isinstance(post, NewsPost)
    assert post.framing == "Ojo"


async def test_the_request_explicitly_disables_thinking(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """V4 defaults thinking to enabled, which would ignore temperature and bill CoT."""
    fake = FakeDeepSeek(json_output_response(VALID_POST))

    await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume esto", NewsPost)

    assert fake.last_request["thinking"] == {"type": "disabled"}


async def test_the_sampling_knobs_the_persona_needs_are_sent(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """Only meaningful because thinking is off; thinking mode ignores these.

    A loose voice for the prose calls and a repeatable one for the gates, so the
    two must not arrive at DeepSeek as the same number.
    """
    fake = FakeDeepSeek(json_output_response(VALID_POST))
    brain = brain_for(settings, fake, store)

    await brain.ask(CallType.REPLY_GENERATION, "contesta", NewsPost)
    loose = fake.last_request["temperature"]
    await brain.ask(CallType.REPLY_GATE, "clasifica", NewsPost)
    tight = fake.last_request["temperature"]

    assert loose == CALL_SHAPES[CallType.REPLY_GENERATION].temperature
    assert tight == CALL_SHAPES[CallType.REPLY_GATE].temperature
    assert loose > tight


async def test_every_call_carries_a_max_tokens(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """Under the name DeepSeek documents: a cap the provider ignores is no cap."""
    fake = FakeDeepSeek(json_output_response(VALID_POST))
    brain = brain_for(settings, fake, store)

    for call_type in CallType:
        await brain.ask(call_type, "algo", NewsPost)
        body = fake.last_request
        assert body["max_tokens"] == CALL_SHAPES[call_type].max_tokens
        assert "max_completion_tokens" not in body


async def test_a_caller_cannot_ask_for_an_uncapped_call(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """The cap is the call type's, not the caller's; there is no knob to forget."""
    fake = FakeDeepSeek(json_output_response(VALID_POST))

    with pytest.raises(TypeError):
        await brain_for(settings, fake, store).ask(
            CallType.NEWS_SUMMARY,
            "resume",
            NewsPost,
            max_tokens=None,  # type: ignore[call-arg]
        )


async def test_the_current_non_thinking_model_is_used(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """`deepseek-chat` was retired 2026/07/24; `deepseek-v4-flash` replaced it."""
    fake = FakeDeepSeek(json_output_response(VALID_POST))

    await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert fake.last_request["model"] == DEEPSEEK_MODEL == "deepseek-v4-flash"


async def test_the_typed_answer_is_asked_for_as_json_and_not_as_a_tool_call(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """The reply gate died in production on a tool call whose arguments were the
    model thinking out loud. V4 will not reliably stop thinking and will not take
    a forced `tool_choice`, so the tool is out of the path: the schema goes in the
    instructions and the answer comes back as the message body."""
    fake = FakeDeepSeek(json_output_response(VALID_POST))

    await brain_for(settings, fake, store).ask(CallType.REPLY_GATE, "clasifica", NewsPost)

    body = fake.last_request
    assert body["response_format"] == {"type": "json_object"}
    assert "tools" not in body
    assert "tool_choice" not in body


async def test_reasoning_beside_the_answer_is_not_part_of_the_answer(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """Thinking mode has its own field. A model that fills it and still answers
    properly must not be treated as a failure, which is what happened when the
    chain-of-thought was landing in the payload itself."""
    fake = FakeDeepSeek(json_output_response(VALID_POST, reasoning="Let me analyze this message."))

    post = await brain_for(settings, fake, store).ask(CallType.REPLY_GATE, "clasifica", NewsPost)

    assert post.framing == "Ojo"


async def test_a_response_that_fails_validation_is_a_failure_not_a_half_object(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    fake = FakeDeepSeek(json_output_response('{"framing": "Ojo"}'))

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)


async def test_a_validation_failure_says_which_field_was_wrong(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """Pydantic AI's own message names the retry policy and not the fault, so a
    caller that logged only that would have to deploy again to learn anything.
    `NewsPost` wants a `line`, and the failure has to say so."""
    fake = FakeDeepSeek(json_output_response('{"framing": "Ojo"}'))

    with pytest.raises(BrainCallError) as caught:
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert "line" in str(caught.value)


async def test_a_failure_detail_cannot_grow_without_bound(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """The detail reaches a Telegram alert, so a model that answered with an essay
    must not be quoted back in full."""
    fake = FakeDeepSeek(json_output_response('{"framing": "%s"}' % ("ojo " * 2000)))

    with pytest.raises(BrainCallError) as caught:
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert len(str(caught.value)) < 1000


async def test_an_unusable_answer_earns_one_more_try(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """The 2026-07-28 incident: DeepSeek answered 200 with whitespace in `content`
    three times in twenty minutes, and three tier-one replies died of it - while
    the same request minutes later answered fine. A blank completion gets one
    second chance, and the retry is reserved and billed like any other call."""
    fake = FakeDeepSeek(json_output_response(" \n"), json_output_response(VALID_POST))

    post = await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert post.framing == "Ojo"
    assert len(fake.requests) == 2
    assert await store.calls_on(TODAY) == 2


async def test_a_second_unusable_answer_is_a_failure(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """One retry, not a loop: two blanks in a row is a bad provider, not bad luck."""
    fake = FakeDeepSeek(json_output_response(" \n"))

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert len(fake.requests) == 2
    assert await store.calls_on(TODAY) == 2


async def test_a_transport_failure_is_not_retried(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """The retry is for an answer that came back unusable. A request that never
    got an answer at all fails once, as it always has."""
    fake = FakeDeepSeek(500)

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert len(fake.requests) == 1


async def test_a_validation_failure_is_retried_once_and_every_attempt_is_counted(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """One logical call used to be one request, so the counter could detect a
    loop. The blank-completion retry changed the shape of that guarantee, not
    its substance: every attempt reserves its own call against the day's
    ceiling, so a runaway still cannot hide from the counter."""
    fake = FakeDeepSeek(json_output_response('{"framing": "Ojo"}'))

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert len(fake.requests) == 2
    assert await store.calls_on(TODAY) == 2


async def test_a_failed_call_still_books_the_tokens_it_was_billed_for(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """A call that fails validation was still generated, and still cost money -
    on the first attempt and on the retry alike."""
    fake = FakeDeepSeek(
        json_output_response(
            '{"framing": "Ojo"}',
            usage={
                "prompt_tokens": 1000,
                "completion_tokens": 150,
                "total_tokens": 1150,
                "prompt_cache_hit_tokens": 700,
                "prompt_cache_miss_tokens": 300,
            },
        )
    )

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert (await store.totals_on(TODAY))[CallType.NEWS_SUMMARY] == DayTotals(
        calls=2, cache_hit_tokens=1400, cache_miss_tokens=600, completion_tokens=300
    )


async def test_a_call_that_never_reached_deepseek_books_no_tokens(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    fake = FakeDeepSeek(500)

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    totals = (await store.totals_on(TODAY))[CallType.NEWS_SUMMARY]
    assert totals.calls == 1
    assert totals.completion_tokens == 0


async def test_a_transport_failure_surfaces_to_the_caller(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    fake = FakeDeepSeek(500)

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)


async def test_a_failed_call_tells_the_maintainer_out_of_band(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """The group is told nothing - the item is dropped, which is the fail-silent
    rule of the reply policy - so the maintainer has to hear about it somewhere,
    and Telegram is the somewhere."""
    alerter = RecordingAlerter()
    fake = FakeDeepSeek(500)

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store, alerter).ask(
            CallType.NEWS_SUMMARY, "resume", NewsPost
        )

    (alert,) = alerter.messages
    assert Signal.BRAIN_ERROR in alert
    assert CallType.NEWS_SUMMARY in alert


async def test_the_days_ceiling_is_not_alerted_twice_over(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """The guard already says the day is spent, in its own words. A second alert
    calling the same thing a DeepSeek error would be noise about nothing broken."""
    alerter = RecordingAlerter()
    store.seed(TODAY, CallType.NEWS_SUMMARY, DayTotals(calls=STOP_THRESHOLD))

    with pytest.raises(BrainStoppedError):
        await brain_for(settings, FakeDeepSeek(), store, alerter).ask(
            CallType.NEWS_SUMMARY, "resume", NewsPost
        )

    assert not any(Signal.BRAIN_ERROR in alert for alert in alerter.messages)


async def test_reported_usage_is_persisted_per_day_and_call_type(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    fake = FakeDeepSeek(
        json_output_response(
            VALID_POST,
            usage={
                "prompt_tokens": 1000,
                "completion_tokens": 150,
                "total_tokens": 1150,
                "prompt_cache_hit_tokens": 700,
                "prompt_cache_miss_tokens": 300,
            },
        )
    )

    await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    totals = (await store.totals_on(TODAY))[CallType.NEWS_SUMMARY]
    assert totals == DayTotals(
        calls=1, cache_hit_tokens=700, cache_miss_tokens=300, completion_tokens=150
    )


async def test_usage_without_cache_fields_is_booked_as_a_full_miss(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """A response that omits the cache split must not quietly record zeros."""
    fake = FakeDeepSeek(
        json_output_response(
            VALID_POST,
            usage={"prompt_tokens": 400, "completion_tokens": 30, "total_tokens": 430},
        )
    )

    await brain_for(settings, fake, store).ask(CallType.REPLY_GATE, "clasifica", NewsPost)

    totals = (await store.totals_on(TODAY))[CallType.REPLY_GATE]
    assert totals == DayTotals(
        calls=1, cache_hit_tokens=0, cache_miss_tokens=400, completion_tokens=30
    )


async def test_calls_of_different_types_are_counted_apart(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    fake = FakeDeepSeek(json_output_response(VALID_POST))
    brain = brain_for(settings, fake, store)

    await brain.ask(CallType.NEWS_SUMMARY, "resume", NewsPost)
    await brain.ask(CallType.REPLY_GATE, "clasifica", NewsPost)
    await brain.ask(CallType.REPLY_GATE, "clasifica", NewsPost)

    totals = await store.totals_on(TODAY)
    assert totals[CallType.NEWS_SUMMARY].calls == 1
    assert totals[CallType.REPLY_GATE].calls == 2
    assert await store.calls_on(TODAY) == 3


async def test_a_stopped_day_makes_no_request_at_all(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    store.seed(TODAY, CallType.REPLY_GATE, DayTotals(calls=STOP_THRESHOLD))
    fake = FakeDeepSeek(json_output_response(VALID_POST))

    with pytest.raises(BrainStoppedError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert fake.requests == []


def unusable_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The per-attempt WARNING lines the brain logs for an answer that will not parse."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "rebe_agent.brain" and "came back unusable:" in record.getMessage()
    ]


async def test_a_blank_answer_is_logged_with_finish_reason_and_exact_content(
    settings: Settings, store: InMemoryUsageStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The 12:44 reply died as `Invalid JSON` over an input of `' '` and the log
    could not say why. The per-attempt line has to carry the provider's own
    `finish_reason` and the content as it arrived - quoted, so a space is visible,
    and with its length, so one space and none are not the same line."""
    fake = FakeDeepSeek(json_output_response(" \n", finish_reason="length"))

    with (
        caplog.at_level(logging.WARNING, logger="rebe_agent.brain"),
        pytest.raises(BrainCallError),
    ):
        await brain_for(settings, fake, store).ask(CallType.REPLY_GENERATION, "contesta", NewsPost)

    lines = unusable_warnings(caplog)
    assert len(lines) == 2  # one per attempt, not just once at the end
    for line in lines:
        assert "finish_reason='length'" in line
        assert "text[2]=' \\n'" in line


async def test_a_reasoning_only_answer_is_logged_apart_from_an_empty_one(
    settings: Settings, store: InMemoryUsageStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A body that is blank because the whole generation went into
    `reasoning_content` needs a different fix than a body that is blank because
    the provider sent nothing, so the two must not log as the same shape."""
    thinking_fake = FakeDeepSeek(json_output_response("", reasoning="Let me think about this."))
    with (
        caplog.at_level(logging.WARNING, logger="rebe_agent.brain"),
        pytest.raises(BrainCallError),
    ):
        await brain_for(settings, thinking_fake, store).ask(
            CallType.REPLY_GENERATION, "contesta", NewsPost
        )
    thinking_lines = unusable_warnings(caplog)
    assert thinking_lines
    for line in thinking_lines:
        assert "thinking[24]='Let me think about this.'" in line
        assert "text[" not in line

    caplog.clear()
    empty_fake = FakeDeepSeek(json_output_response(" "))
    with pytest.raises(BrainCallError):
        await brain_for(settings, empty_fake, store).ask(
            CallType.REPLY_GENERATION, "contesta", NewsPost
        )
    empty_lines = unusable_warnings(caplog)
    assert empty_lines
    for line in empty_lines:
        assert "text[1]=' '" in line
        assert "thinking[" not in line


async def test_the_unusable_shape_line_stays_bounded(
    settings: Settings, store: InMemoryUsageStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The line quotes what the model sent, and the model can send an essay.
    The length survives the cut - it rides in front of the content - but the
    content itself does not."""
    fake = FakeDeepSeek(json_output_response("x" * 5000))

    with (
        caplog.at_level(logging.WARNING, logger="rebe_agent.brain"),
        pytest.raises(BrainCallError),
    ):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    lines = unusable_warnings(caplog)
    assert lines
    for line in lines:
        assert "text[5000]=" in line
        assert len(line) < 700
        assert line.endswith("...")


MANGLED_NEWS_BODY = '{"text":":"ya leiste sobre DeltaNet y sus variantes de atencion lineal?"}'
"""The 2026-07-28 incident body, verbatim from the production log.

DeepSeek's JSON mode opened the value string, wrote a lone `:` where the post
was meant to begin (and the inverted question mark was never written), closed
the string, and wrote the whole post as bare text after it. `finish_reason`
was `stop` and every word of the answer arrived inside the broken envelope.
"""


async def test_an_answer_inside_a_broken_envelope_is_recovered(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """A news_summary call died on 2026-07-28 as `Invalid JSON` over
    `MANGLED_NEWS_BODY`, on both attempts, and the post was lost though every
    word of it had arrived. The envelope is not the answer: when the schema is
    one text field and the JSON around it is broken, the text is recovered and
    validated, and the run keeps its post.

    The real `NewsPost` rather than the stand-in above, because the field count
    is exactly what this behaviour turns on."""
    fake = FakeDeepSeek(json_output_response(MANGLED_NEWS_BODY))

    post = await brain_for(settings, fake, store).ask(
        CallType.NEWS_SUMMARY, "resume esto", news.NewsPost
    )

    assert post.text == "ya leiste sobre DeltaNet y sus variantes de atencion lineal?"


async def test_a_clean_second_sample_wins_over_a_recovered_first(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """The retry exists because a fresh sample of a flaky provider is cheap
    insurance; recovery is for when the fresh sample glitches the same way,
    never a reason to skip asking again."""
    fake = FakeDeepSeek(
        json_output_response(MANGLED_NEWS_BODY),
        json_output_response('{"text": "ojo con DeltaNet y la atencion lineal"}'),
    )

    post = await brain_for(settings, fake, store).ask(
        CallType.NEWS_SUMMARY, "resume esto", news.NewsPost
    )

    assert post.text == "ojo con DeltaNet y la atencion lineal"


async def test_a_broken_envelope_for_a_multi_field_answer_still_fails(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """Recovery reads the one field there is. With more than one, which string
    was which is guesswork, and a guessed answer is worse than a dropped call."""
    fake = FakeDeepSeek(
        json_output_response('{"framing":":"Ojo", "line": "Sale.", "url": "https://x.mx/a"}')
    )

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)


async def test_a_broken_envelope_without_the_answer_inside_still_fails(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """A truncated generation holds no complete string to recover - the key is
    the only literal - so the call fails as before. Recovery is for answers
    that arrived, not for inventing ones that did not."""
    fake = FakeDeepSeek(json_output_response('{"text":"ya leiste'))

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", news.NewsPost)
