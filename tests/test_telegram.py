"""What the alert channel puts on the wire, and what it never says out loud."""

from __future__ import annotations

import pytest

from rebe_agent.telegram import TelegramClient, TelegramError
from tests.telegram_stub import CHAT_ID, TOKEN, FakeTelegram, message


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


def client_for(fake: FakeTelegram) -> TelegramClient:
    return TelegramClient(TOKEN, CHAT_ID, http_client=fake.client())


async def test_an_alert_goes_to_the_configured_chat(telegram: FakeTelegram) -> None:
    await client_for(telegram).send_message("Rebe no puede enviar")

    assert telegram.texts == ["Rebe no puede enviar"]
    assert telegram.calls[0].chat_id == CHAT_ID
    assert telegram.calls[0].method == "sendMessage"


async def test_the_bot_token_travels_in_the_path_where_telegram_wants_it(
    telegram: FakeTelegram,
) -> None:
    await client_for(telegram).send_message("algo")

    assert telegram.calls[0].path == f"/bot{TOKEN}/sendMessage"


async def test_a_refused_call_is_an_error_that_never_prints_the_token(
    telegram: FakeTelegram,
) -> None:
    """The token is in the URL, so any error carrying a URL is a leaked secret."""
    telegram.status = 401

    with pytest.raises(TelegramError) as failed:
        await client_for(telegram).send_message("algo")

    assert TOKEN not in str(failed.value)
    assert failed.value.status == 401


async def test_polling_reads_the_messages_a_maintainer_sent(telegram: FakeTelegram) -> None:
    telegram.updates = [[message(41, "/pausa"), message(42, "/reanuda")]]

    updates = await client_for(telegram).poll(offset=0)

    assert [update.text for update in updates] == ["/pausa", "/reanuda"]
    assert [update.update_id for update in updates] == [41, 42]
    assert {update.chat_id for update in updates} == {CHAT_ID}


async def test_polling_asks_only_for_messages_and_waits_for_them(telegram: FakeTelegram) -> None:
    """A long poll is what makes the control channel instant without hammering."""
    await client_for(telegram).poll(offset=7)

    assert telegram.polls[0]["offset"] == 7
    assert telegram.polls[0]["timeout"] > 0
    assert telegram.polls[0]["allowed_updates"] == ["message"]


async def test_an_update_that_is_not_a_text_message_is_skipped(telegram: FakeTelegram) -> None:
    """Photos, joins and edits are not commands, and must not shift the offset
    onto something the control channel cannot answer."""
    telegram.updates = [[{"update_id": 5}, {"update_id": 6, "message": {"chat": {"id": 1}}}]]

    assert await client_for(telegram).poll(offset=0) == []
