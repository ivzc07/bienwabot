"""The transport: the two endpoints, the key, and how a rejection comes back."""

from __future__ import annotations

import httpx
import pytest

from rebe_agent.config import load_settings
from rebe_agent.evolution import (
    COMPOSING,
    PAUSED,
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


async def test_a_photo_goes_to_the_media_endpoint_with_its_caption(
    client: EvolutionClient, evolution: FakeEvolution
) -> None:
    """The body is the whole contract: WhatsApp fetches the image itself, so
    `media` is a URL rather than bytes, and her words ride along as `caption`."""
    image = "https://openai.com/og/local-model.png"
    caption = "Miren, salio un modelo que corre sin nube\nhttps://openai.com/index/local-model"

    message_id = await client.send_media(GROUP, image, caption)

    assert message_id == evolution.message_id
    call = evolution.calls[-1]
    assert call.path == f"/message/sendMedia/{INSTANCE}"
    assert call.body == {
        "number": GROUP,
        "mediatype": "image",
        "media": image,
        "caption": caption,
    }
    assert call.api_key == API_KEY


async def test_a_presence_carries_its_hold_in_milliseconds(
    client: EvolutionClient, evolution: FakeEvolution
) -> None:
    """`delay` is required by the endpoint and is how long Evolution holds the
    presence up. Sending seconds where it wants milliseconds would be a typing
    indicator that blinks out in three."""
    await client.send_presence(GROUP, COMPOSING, 3.2)

    call = evolution.calls[-1]
    assert call.path == f"/chat/sendPresence/{INSTANCE}"
    assert call.body == {"number": GROUP, "presence": COMPOSING, "delay": 3200}


async def test_clearing_a_presence_holds_it_for_nothing(
    client: EvolutionClient, evolution: FakeEvolution
) -> None:
    """Nothing waits on the presence that follows a send, but the field is still
    required, and zero is the truthful value for it."""
    await client.send_presence(GROUP, PAUSED)

    assert evolution.calls[-1].body == {"number": GROUP, "presence": PAUSED, "delay": 0}


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
