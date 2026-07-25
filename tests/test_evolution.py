"""The transport: the two endpoints, the key, and how a rejection comes back."""

from __future__ import annotations

import httpx
import pytest

from rebe_agent.config import load_settings
from rebe_agent.evolution import (
    COMPOSING,
    EvolutionClient,
    EvolutionError,
    EvolutionRateLimitedError,
    build_client,
)
from tests.evolution_stub import API_KEY, BASE_URL, INSTANCE, FakeEvolution
from tests.support import GROUP
from tests.test_config import COMPLETE_ENV


@pytest.fixture
def evolution() -> FakeEvolution:
    return FakeEvolution()


@pytest.fixture
def client(evolution: FakeEvolution) -> EvolutionClient:
    return EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client())


async def test_a_text_goes_to_the_documented_endpoint_and_answers_with_its_id(
    client: EvolutionClient, evolution: FakeEvolution
) -> None:
    message_id = await client.send_text(GROUP, "hola")

    assert message_id == evolution.message_id
    call = evolution.calls[-1]
    assert call.path == f"/message/sendText/{INSTANCE}"
    assert call.body == {"number": GROUP, "text": "hola"}
    assert call.api_key == API_KEY


async def test_presence_goes_out_without_asking_evolution_to_sleep(
    client: EvolutionClient, evolution: FakeEvolution
) -> None:
    """The pause belongs to the pacer, which refreshes this presence while it runs.
    A `delay` here would hand that timing to an HTTP call nobody can test."""
    await client.send_presence(GROUP, COMPOSING)

    call = evolution.calls[-1]
    assert call.path == f"/chat/sendPresence/{INSTANCE}"
    assert call.body == {"number": GROUP, "presence": COMPOSING}
    assert "delay" not in call.body


async def test_a_reach_out_time_lock_has_its_own_type(evolution: FakeEvolution) -> None:
    """463 is WhatsApp pushing back. The answer is back off and alert, never retry."""
    evolution.status = 463
    client = EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client())

    with pytest.raises(EvolutionRateLimitedError) as caught:
        await client.send_text(GROUP, "hola")

    assert caught.value.status == 463


async def test_a_rate_limit_is_still_an_evolution_error(evolution: FakeEvolution) -> None:
    evolution.status = 429
    client = EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client())

    with pytest.raises(EvolutionError):
        await client.send_text(GROUP, "hola")


async def test_a_server_error_names_what_failed(evolution: FakeEvolution) -> None:
    evolution.status = 500
    client = EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client())

    with pytest.raises(EvolutionError, match="send a message") as caught:
        await client.send_text(GROUP, "hola")

    assert caught.value.status == 500
    assert not isinstance(caught.value, EvolutionRateLimitedError)


async def test_a_connection_that_never_opens_is_the_same_kind_of_failure() -> None:
    """Evolution being unreachable and Evolution saying no are one problem to a
    caller: the message did not get out."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = EvolutionClient(
        BASE_URL,
        API_KEY,
        INSTANCE,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )

    with pytest.raises(EvolutionError) as caught:
        await client.send_presence(GROUP, COMPOSING)

    assert caught.value.status is None


async def test_a_response_without_a_key_is_not_a_failure(evolution: FakeEvolution) -> None:
    """An unknown message ID is worth logging, not worth failing a landed send over."""
    evolution.message_id = ""
    client = EvolutionClient(BASE_URL, API_KEY, INSTANCE, http_client=evolution.client())

    assert await client.send_text(GROUP, "hola") == ""


async def test_the_client_is_built_from_the_configured_instance() -> None:
    """Failover is a configuration change, so the instance is read in one place."""
    evolution = FakeEvolution()
    settings = load_settings(dict(COMPLETE_ENV, EVOLUTION_INSTANCE="bien-backup"))

    async with build_client(settings, http_client=evolution.client()) as client:
        await client.send_text(GROUP, "hola")

    assert evolution.calls[-1].path == "/message/sendText/bien-backup"
    assert evolution.calls[-1].api_key == COMPLETE_ENV["EVOLUTION_API_KEY"]
