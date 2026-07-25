"""What the brain puts on the wire, and what it refuses to hand back."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

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
from rebe_agent.usage import CallType, DayTotals, InMemoryUsageStore
from tests.deepseek_stub import FakeDeepSeek, tool_call_response
from tests.test_config import COMPLETE_ENV

MEXICO_CITY = ZoneInfo("America/Mexico_City")
NOON = datetime(2026, 7, 25, 12, 0, tzinfo=MEXICO_CITY)
TODAY = NOON.date()


class NewsPost(BaseModel):
    """Stands in for the real news schema; any typed output exercises the path."""

    framing: str
    line: str
    url: str


VALID_POST = '{"framing": "Ojo", "line": "Sale un modelo nuevo.", "url": "https://x.mx/a"}'


class RecordingAlerter:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def alert(self, message: str) -> None:
        self.messages.append(message)


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
    fake = FakeDeepSeek(tool_call_response(VALID_POST))

    post = await brain_for(settings, fake, store).ask(
        CallType.NEWS_SUMMARY, "resume esto", NewsPost
    )

    assert isinstance(post, NewsPost)
    assert post.framing == "Ojo"


async def test_the_request_explicitly_disables_thinking(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """V4 defaults thinking to enabled, which would ignore temperature and bill CoT."""
    fake = FakeDeepSeek(tool_call_response(VALID_POST))

    await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume esto", NewsPost)

    assert fake.last_request["thinking"] == {"type": "disabled"}


async def test_the_sampling_knobs_the_persona_needs_are_sent(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """Only meaningful because thinking is off; thinking mode ignores these."""
    fake = FakeDeepSeek(tool_call_response(VALID_POST))

    await brain_for(settings, fake, store).ask(
        CallType.REPLY_GENERATION, "contesta", NewsPost, temperature=0.9
    )

    assert fake.last_request["temperature"] == 0.9


async def test_every_call_carries_a_max_tokens(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """Under the name DeepSeek documents: a cap the provider ignores is no cap."""
    fake = FakeDeepSeek(tool_call_response(VALID_POST))
    brain = brain_for(settings, fake, store)

    for call_type in CallType:
        await brain.ask(call_type, "algo", NewsPost)
        body = fake.last_request
        assert body["max_tokens"] == CALL_SHAPES[call_type].max_tokens
        assert "max_completion_tokens" not in body


async def test_an_explicit_max_tokens_overrides_the_call_shape(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    fake = FakeDeepSeek(tool_call_response(VALID_POST))

    await brain_for(settings, fake, store).ask(
        CallType.NEWS_SUMMARY, "resume", NewsPost, max_tokens=64
    )

    assert fake.last_request["max_tokens"] == 64


async def test_the_current_non_thinking_model_is_used(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """`deepseek-chat` was retired 2026/07/24; `deepseek-v4-flash` replaced it."""
    fake = FakeDeepSeek(tool_call_response(VALID_POST))

    await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert fake.last_request["model"] == DEEPSEEK_MODEL == "deepseek-v4-flash"


async def test_a_response_that_fails_validation_is_a_failure_not_a_half_object(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    fake = FakeDeepSeek(tool_call_response('{"framing": "Ojo"}'))

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)


async def test_a_validation_failure_is_not_retried_into_a_second_billed_call(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    """One logical call is one request, or the counter cannot detect a loop."""
    fake = FakeDeepSeek(tool_call_response('{"framing": "Ojo"}'))

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert len(fake.requests) == 1
    assert await store.calls_on(TODAY) == 1


async def test_a_transport_failure_surfaces_to_the_caller(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    fake = FakeDeepSeek(500)

    with pytest.raises(BrainCallError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)


async def test_reported_usage_is_persisted_per_day_and_call_type(
    settings: Settings, store: InMemoryUsageStore
) -> None:
    fake = FakeDeepSeek(
        tool_call_response(
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
        tool_call_response(
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
    fake = FakeDeepSeek(tool_call_response(VALID_POST))
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
    fake = FakeDeepSeek(tool_call_response(VALID_POST))

    with pytest.raises(BrainStoppedError):
        await brain_for(settings, fake, store).ask(CallType.NEWS_SUMMARY, "resume", NewsPost)

    assert fake.requests == []
