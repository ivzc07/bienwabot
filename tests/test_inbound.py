"""The reply gate, read off real webhook bodies.

Three things are under test and they are deliberately separate: `parse`, which
turns Evolution's body into something typed or into nothing at all; `tier`, which
is the three-tier decision from the reply policy; and `parse_connection`, which
reads the other event each instance subscribes to. Everything here is mechanical
- no model is asked whether a name-tag is a name-tag.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rebe_agent.inbound import Tier, parse, parse_connection, tier
from tests.support import GROUP
from tests.webhooks import ANA, AT_EPOCH, BETO, REBE, edited, payload

HER_POST = "STUB-MESSAGE-ID"
"""The id of a message Rebe sent, as the send path would have remembered it."""


def tier_of(name: str, *, hers: frozenset[str] = frozenset()) -> Tier:
    message = parse(payload(name))
    assert message is not None, f"{name} did not parse"
    return tier(message, hers=hers)


# --- what a webhook body becomes ---------------------------------------------


def test_a_group_message_carries_everything_the_leg_needs() -> None:
    message = parse(payload("by_name"))

    assert message is not None
    assert message.chat == GROUP
    assert message.author == ANA
    assert message.author_name == "Ana"
    assert message.text == "Rebe, que opinas de los modelos que corren local?"
    assert message.message_id == "3EB0A1B2C3D4E5F60002"
    assert message.at == datetime(2026, 7, 25, 18, 0, tzinfo=UTC)
    assert message.rebe == REBE


def test_a_mention_arrives_as_a_jid_not_only_as_text() -> None:
    message = parse(payload("mention"))

    assert message is not None
    assert message.mentioned == frozenset({REBE})


def test_a_quote_carries_the_id_of_the_message_it_answers() -> None:
    message = parse(payload("quote"))

    assert message is not None
    assert message.quoted_id == HER_POST
    assert message.quoted_author == REBE


def test_a_caption_is_the_text_of_a_photo() -> None:
    """The reply policy treats media *with* readable text as text, and only
    media without any as something to stay quiet about."""
    message = parse(payload("caption"))

    assert message is not None
    assert message.text == "rebe que opinas de esta grafica"


def test_an_event_that_is_not_an_inbound_message_is_not_a_message() -> None:
    assert parse(payload("connection_update")) is None


@pytest.mark.parametrize("body", [{}, {"event": "messages.upsert"}, {"data": {"key": {}}}])
def test_a_body_that_is_not_shaped_like_a_message_is_refused(body: dict[str, object]) -> None:
    """Nothing that arrives on an open port is trusted to have the fields it should."""
    assert parse(body) is None


# --- tier one: addressed ------------------------------------------------------


@pytest.mark.parametrize("name", ["mention", "by_name", "bot_question", "caption"])
def test_a_name_tag_in_any_form_is_addressed(name: str) -> None:
    assert tier_of(name) is Tier.ADDRESSED


def test_a_quote_of_one_of_her_messages_is_addressed() -> None:
    """She is answered without ever being named, which is how WhatsApp replies work."""
    assert tier_of("quote", hers=frozenset({HER_POST})) is Tier.ADDRESSED


def test_a_quote_of_somebody_elses_message_is_not() -> None:
    message = parse(edited("quote", quoted_id="3EB0SOMEONEELSE", quoted_author=BETO))
    assert message is not None

    assert tier(message, hers=frozenset({HER_POST})) is Tier.CHATTER


def test_her_name_inside_a_longer_word_is_not_a_name_tag() -> None:
    """ "rebeldes" is not "Rebe". A gate that fires on a substring would have her
    answering half the group's vocabulary."""
    message = parse(edited("by_name", text="los rebeldes ya llegaron al estadio"))
    assert message is not None

    assert tier(message, hers=frozenset()) is Tier.CHATTER


@pytest.mark.parametrize(
    "text",
    ["rebe que onda", "REBE ya viste esto?", "oye Rebé, que opinas", "que dices rebe"],
)
def test_her_name_is_recognised_however_it_is_typed(text: str) -> None:
    message = parse(edited("by_name", text=text))
    assert message is not None

    assert tier(message, hers=frozenset()) is Tier.ADDRESSED


# --- tier two and three -------------------------------------------------------


def test_unaddressed_chatter_is_tier_two() -> None:
    """Whether she chimes in is a later ticket's decision; this leg only has to
    stop calling it addressed."""
    assert tier_of("chatter") is Tier.CHATTER


def test_small_talk_is_tier_two_as_well() -> None:
    """Tier two is "not addressed", not "about AI" - which topic it is belongs to
    the chime-in ticket, and this gate deliberately does not guess."""
    assert tier_of("small_talk") is Tier.CHATTER


def test_a_sticker_with_no_words_is_silence() -> None:
    """She does not guess at what a picture or a voice note said."""
    assert tier_of("sticker") is Tier.SILENT


def test_her_own_messages_are_never_answered() -> None:
    """Evolution echoes her sends back through the same webhook. Answering them
    is the runaway loop the call guard exists to catch, one step earlier."""
    assert tier_of("from_rebe") is Tier.SILENT


def test_a_direct_message_is_silence_however_it_is_worded() -> None:
    """The consent spec is explicit: she never DMs a member, so a name-tag in a
    private chat is not an invitation."""
    assert tier_of("direct_message") is Tier.SILENT


def test_a_mention_of_somebody_else_is_not_a_mention_of_her() -> None:
    message = parse(edited("mention", text="@5215551112222 ya viste?", mentioned=[BETO]))
    assert message is not None

    assert tier(message, hers=frozenset()) is Tier.CHATTER


def test_the_recorded_timestamp_is_the_one_the_shape_rules_read() -> None:
    later = parse(edited("by_name", at_epoch=AT_EPOCH + 600))

    assert later is not None
    assert later.at == datetime(2026, 7, 25, 18, 10, tzinfo=UTC)


# --- the other event: what Evolution says about the link ----------------------


def test_a_connection_update_is_read_off_the_recorded_payload() -> None:
    update = parse_connection(payload("connection_update"))

    assert update is not None
    assert update.state == "open"
    assert update.reason == 200


def test_a_disconnect_carries_the_reason_baileys_named() -> None:
    """401 and 403 are what tell a dropped socket from a banned number, so the
    reason has to survive the parse."""
    body = {"event": "connection.update", "data": {"state": "close", "statusReason": 403}}

    update = parse_connection(body)

    assert update is not None
    assert (update.state, update.reason) == ("close", 403)


def test_a_reason_that_arrived_as_a_string_is_still_a_number() -> None:
    body = {"event": "connection.update", "data": {"state": "close", "statusCode": "401"}}

    update = parse_connection(body)

    assert update is not None and update.reason == 401


def test_a_disconnect_with_no_reason_is_still_a_disconnect() -> None:
    update = parse_connection({"event": "connection.update", "data": {"state": "close"}})

    assert update is not None and update.reason is None


@pytest.mark.parametrize(
    "body",
    [
        {"event": "connection.update", "data": {"state": ""}},
        {"event": "connection.update", "data": {}},
        {"event": "connection.update"},
        {"event": "connection.update", "data": {"state": {"nested": "nonsense"}}},
        {"nothing": "useful"},
    ],
)
def test_a_body_with_no_readable_state_is_not_a_connection_update(
    body: dict[str, object],
) -> None:
    """Guessing that a malformed delivery meant "the link is down" would let
    anything that can reach the port stop Rebe sending."""
    assert parse_connection(body) is None


def test_an_inbound_message_is_not_a_connection_update() -> None:
    assert parse_connection(payload("by_name")) is None
