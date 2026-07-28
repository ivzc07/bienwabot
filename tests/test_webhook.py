"""The endpoint Evolution posts to: the token, and what gets past it.

Served through an in-process ASGI transport rather than a socket, so the whole
path - route, token check, parse, gate, brain, pacer - runs exactly as it would
in the container, and nothing listens on a port during the test run.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest

from rebe_agent.brain import build_brain
from rebe_agent.chimeins import ChimeInBudget, InMemoryChimeInLog
from rebe_agent.clock import ManualClock, ManualSleeper
from rebe_agent.config import Settings, load_settings
from rebe_agent.evolution import EvolutionClient
from rebe_agent.memory import InMemoryGroupMemory
from rebe_agent.pacer import Envelope, Pacer
from rebe_agent.ramp import InMemoryRampStore, Ramp
from rebe_agent.reply import ReplyLeg
from rebe_agent.sends import InMemorySendLog
from rebe_agent.signals import LinkWatch, Watchtower
from rebe_agent.usage import InMemoryUsageStore
from rebe_agent.webhook import WEBHOOK_PATH, build_app
from tests.deepseek_stub import FakeDeepSeek, json_output_response
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import NOON, RecordingAlerter
from tests.test_config import COMPLETE_ENV
from tests.test_reply import VOICE, verdict, wrote
from tests.webhooks import edited, payload

SECRET = COMPLETE_ENV["WEBHOOK_SECRET"]
"""The same value the process would have read from the environment."""

GOOD = f"/webhook/{SECRET}"


@pytest.fixture
def settings() -> Settings:
    return load_settings(dict(COMPLETE_ENV))


@pytest.fixture
def evolution() -> FakeEvolution:
    return FakeEvolution()


@pytest.fixture
def memory() -> InMemoryGroupMemory:
    return InMemoryGroupMemory()


def make_client(
    settings: Settings,
    fake: FakeDeepSeek,
    evolution: FakeEvolution,
    memory: InMemoryGroupMemory,
    *,
    link: LinkWatch | None = None,
    ramp: Ramp | None = None,
) -> httpx.AsyncClient:
    """The real app over the real leg, reachable without a socket."""
    clock = ManualClock(NOON)
    transport = EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client())
    leg = ReplyLeg(
        build_brain(
            settings, clock, InMemoryUsageStore(), RecordingAlerter(), http_client=fake.client()
        ),
        Pacer(
            transport,
            InMemorySendLog(),
            clock,
            envelope=Envelope(post_gap=(timedelta(0), timedelta(0))),
            sleeper=ManualSleeper(clock),
            rng=random.Random(20260725),
            ramp=ramp,
        ),
        transport,
        memory,
        ChimeInBudget(InMemoryChimeInLog(), clock, rng=random.Random(20260725)),
    )
    app = build_app(leg, SECRET, link=link)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://rebe-agent:8000"
    )


class RecordingLink:
    """A link watch that keeps what it was told, so a test can read the dispatch."""

    def __init__(self) -> None:
        self.changes: list[tuple[str, int | None]] = []

    async def connection_changed(self, state: str, *, reason: int | None = None) -> None:
        self.changes.append((state, reason))


@pytest.fixture
async def client(
    settings: Settings, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(settings, FakeDeepSeek(verdict(), wrote()), evolution, memory) as opened:
        yield opened


# --- the token ----------------------------------------------------------------


async def test_the_documented_path_is_the_one_that_is_served() -> None:
    """The secret goes in the path, per section 4 of the deployment spec, so the
    route shape is part of the contract with Evolution's per-instance webhook."""
    assert WEBHOOK_PATH == "/webhook/{token}"


async def test_a_correct_token_is_accepted(client: httpx.AsyncClient) -> None:
    response = await client.post(GOOD, json=payload("by_name"))

    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/webhook/wrong-secret",
        "/webhook/",
        "/webhook",
        "/webhook/webhook-secret-test-and-a-bit",
        "/webhook/WEBHOOK-SECRET-TEST",
    ],
)
async def test_a_wrong_or_missing_token_is_rejected_and_nothing_happens(
    path: str, client: httpx.AsyncClient, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> None:
    """Not 403: a wrong token is told the path does not exist, so a scan of the
    internal network learns nothing from the answer."""
    response = await client.post(path, json=payload("by_name"))

    assert response.status_code == 404
    assert evolution.calls == []
    assert memory.turns == [], "a rejected request is not even remembered"


async def test_the_token_is_not_leaked_back_in_the_answer(client: httpx.AsyncClient) -> None:
    response = await client.post("/webhook/wrong-secret", json=payload("by_name"))

    assert SECRET not in response.text
    assert "wrong-secret" not in response.text


async def test_the_endpoint_only_answers_posts(client: httpx.AsyncClient) -> None:
    assert (await client.get(GOOD)).status_code == 405


# --- what a good delivery does ------------------------------------------------


async def test_an_addressed_message_arrives_as_a_paced_reply(
    client: httpx.AsyncClient, evolution: FakeEvolution
) -> None:
    """The acceptance criterion end to end, over HTTP: webhook in, reply out."""
    response = await client.post(GOOD, json=payload("by_name"))

    assert response.status_code == 200
    assert evolution.shape == ["read", "composing", "text", "paused"]
    assert evolution.texts == [VOICE]


async def test_an_unaddressed_message_is_accepted_and_answered_with_silence(
    client: httpx.AsyncClient, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> None:
    """Evolution is told the delivery worked, because it did. Whether Rebe spoke
    is not Evolution's business, and a non-200 would earn a redelivery."""
    response = await client.post(GOOD, json=payload("small_talk"))

    assert response.status_code == 200
    assert evolution.texts == []
    assert len(memory.turns) == 1


async def test_the_same_delivery_twice_produces_one_reply(
    client: httpx.AsyncClient, evolution: FakeEvolution
) -> None:
    """Evolution retries a webhook whose answer it did not see."""
    await client.post(GOOD, json=payload("by_name"))
    await client.post(GOOD, json=payload("by_name"))

    assert len(evolution.texts) == 1


async def test_a_blank_first_answer_still_earns_the_group_a_reply(
    settings: Settings, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> None:
    """The 2026-07-28 incident end to end: DeepSeek's first completion comes
    back blank, the retry answers, and the group gets its reply anyway."""
    fake = FakeDeepSeek(verdict(), json_output_response(" \n"), wrote())
    async with make_client(settings, fake, evolution, memory) as client:
        response = await client.post(GOOD, json=payload("by_name"))

    assert response.status_code == 200
    assert evolution.texts == [VOICE]


@pytest.mark.parametrize(
    "body",
    [
        {"event": "connection.update", "data": {"state": "open"}},
        {"event": "messages.upsert"},
        {"nothing": "useful"},
        [],
    ],
)
async def test_a_body_that_is_not_an_inbound_message_is_shrugged_off(
    body: object, client: httpx.AsyncClient, evolution: FakeEvolution
) -> None:
    """Including bodies nobody sensible would send. A 500 here would make
    Evolution retry a payload that will never work."""
    response = await client.post(GOOD, json=body)

    assert response.status_code == 200
    assert evolution.calls == []


async def test_a_body_that_is_not_json_at_all_is_shrugged_off(
    client: httpx.AsyncClient, evolution: FakeEvolution
) -> None:
    response = await client.post(GOOD, content=b"\x00 not json")

    assert response.status_code == 200
    assert evolution.calls == []


async def test_a_broken_brain_still_answers_the_webhook(
    settings: Settings, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> None:
    """Failure is silence in the group, not a failure on the wire: Evolution has
    done its job and must not be asked to try again."""
    async with make_client(settings, FakeDeepSeek(500), evolution, memory) as client:
        response = await client.post(GOOD, json=payload("by_name"))

    assert response.status_code == 200
    assert evolution.texts == []


async def test_the_group_never_sees_an_error_however_the_request_arrived(
    settings: Settings, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> None:
    """Every shape of bad input in one sweep, checking the one invariant that
    matters: no error text ever reaches the group."""
    bodies = [
        payload("connection_update"),
        edited("by_name", text=""),
        {"event": "messages.upsert", "data": {"key": {"remoteJid": None}}},
        payload("sticker"),
    ]
    async with make_client(settings, FakeDeepSeek(500), evolution, memory) as client:
        for body in bodies:
            assert (await client.post(GOOD, json=body)).status_code == 200

    assert evolution.texts == []


async def test_the_answer_says_nothing_a_caller_could_learn_from(
    client: httpx.AsyncClient,
) -> None:
    """The reply body is deliberately dull: whether Rebe spoke is not something
    an unauthenticated caller on the network should be able to probe for."""
    spoke = await client.post(GOOD, json=payload("by_name"))
    stayed_quiet = await client.post(GOOD, json=payload("small_talk"))

    assert spoke.json() == stayed_quiet.json()


# --- the other event: connection.update ---------------------------------------


async def test_a_connection_update_reaches_the_link_watch(
    settings: Settings, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> None:
    """The deployment spec has each instance subscribe to two events, and until
    this ticket only one of them was wired to anything."""
    link = RecordingLink()
    async with make_client(
        settings, FakeDeepSeek(verdict(), wrote()), evolution, memory, link=link
    ) as client:
        response = await client.post(
            GOOD,
            json={"event": "connection.update", "data": {"state": "close", "statusReason": 401}},
        )

    assert response.status_code == 200
    assert link.changes == [("close", 401)]
    assert evolution.calls == [], "nothing was said to the group about it"


async def test_an_inbound_message_never_reaches_the_link_watch(
    settings: Settings, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> None:
    link = RecordingLink()
    async with make_client(
        settings, FakeDeepSeek(verdict(), wrote()), evolution, memory, link=link
    ) as client:
        await client.post(GOOD, json=payload("by_name"))

    assert link.changes == []
    assert evolution.texts, "the reply leg still had it"


async def test_a_disconnect_silences_the_reply_leg_and_a_reconnect_lets_it_talk(
    settings: Settings, evolution: FakeEvolution, memory: InMemoryGroupMemory
) -> None:
    """End to end through the endpoint Evolution really posts to: the link drops,
    a member addresses her, and nothing goes out until the link is back."""
    ramp = Ramp(InMemoryRampStore(), ManualClock(NOON), InMemorySendLog())
    async with make_client(
        settings,
        FakeDeepSeek(verdict(), wrote(), verdict(), wrote()),
        evolution,
        memory,
        link=Watchtower(RecordingAlerter(), ramp=ramp),
        ramp=ramp,
    ) as client:
        await client.post(GOOD, json={"event": "connection.update", "data": {"state": "close"}})
        await client.post(GOOD, json=payload("by_name"))
        silent = list(evolution.texts)

        await client.post(GOOD, json={"event": "connection.update", "data": {"state": "open"}})
        await client.post(GOOD, json=payload("mention"))

    assert silent == []
    assert len(evolution.texts) == 1
