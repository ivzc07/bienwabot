"""What reaches the maintainer, how often, and what it tells them to do."""

from __future__ import annotations

from datetime import timedelta

import pytest

from rebe_agent.alerts import (
    ALERT_WINDOW,
    Signal,
    TelegramAlerter,
    ThrottledAlerter,
    Watchtower,
)
from rebe_agent.clock import ManualClock
from rebe_agent.evolution import EvolutionError, EvolutionRateLimitedError
from rebe_agent.pause import InMemoryPauseSwitch
from rebe_agent.telegram import TelegramClient
from tests.support import NOON, RecordingAlerter
from tests.telegram_stub import CHAT_ID, TOKEN, FakeTelegram


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOON)


@pytest.fixture
def recorded() -> RecordingAlerter:
    return RecordingAlerter()


@pytest.fixture
def switch(clock: ManualClock) -> InMemoryPauseSwitch:
    return InMemoryPauseSwitch(clock)


# --- The channel -------------------------------------------------------------


async def test_an_alert_reaches_the_maintainer_over_telegram() -> None:
    telegram = FakeTelegram()

    await TelegramAlerter(TelegramClient(TOKEN, CHAT_ID, http_client=telegram.client())).alert(
        "Rebe no puede enviar"
    )

    assert telegram.texts == ["Rebe no puede enviar"]


async def test_an_undeliverable_alert_never_breaks_what_produced_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The alert is about a failure; failing the caller too would hide the first one."""
    telegram = FakeTelegram(status=500)
    alerter = TelegramAlerter(TelegramClient(TOKEN, CHAT_ID, http_client=telegram.client()))

    await alerter.alert("algo se rompio")

    assert "algo se rompio" in caplog.text or "could not" in caplog.text


# --- Rate limiting -----------------------------------------------------------


async def test_repeated_identical_alerts_are_rate_limited(
    recorded: RecordingAlerter, clock: ManualClock
) -> None:
    """An alert storm is the same as no alerts."""
    throttled = ThrottledAlerter(recorded, clock)

    for _ in range(5):
        await throttled.alert("Evolution refused a send")

    assert recorded.messages == ["Evolution refused a send"]


async def test_the_repeats_that_were_held_back_are_counted_into_the_next_alert(
    recorded: RecordingAlerter, clock: ManualClock
) -> None:
    """Rate limiting must not lose the fact that it happened forty more times."""
    throttled = ThrottledAlerter(recorded, clock)
    for _ in range(41):
        await throttled.alert("Evolution refused a send")

    clock.advance(ALERT_WINDOW + timedelta(seconds=1))
    await throttled.alert("Evolution refused a send")

    assert len(recorded.messages) == 2
    assert "40" in recorded.messages[1]


async def test_an_alert_about_something_else_is_not_held_back(
    recorded: RecordingAlerter, clock: ManualClock
) -> None:
    throttled = ThrottledAlerter(recorded, clock)

    await throttled.alert("uno", key="a")
    await throttled.alert("dos", key="b")

    assert recorded.messages == ["uno", "dos"]


async def test_two_reports_of_one_signal_throttle_even_when_the_detail_differs(
    recorded: RecordingAlerter, clock: ManualClock
) -> None:
    """A 463 twice is one signal, and the second body text must not defeat the window."""
    tower = Watchtower(ThrottledAlerter(recorded, clock))

    await tower.send_failed(EvolutionRateLimitedError("send a message", status=463, detail="one"))
    await tower.send_failed(EvolutionRateLimitedError("send a message", status=463, detail="two"))

    assert len(recorded.messages) == 1


# --- What each signal says ---------------------------------------------------


async def test_a_rate_limited_send_names_the_signal(recorded: RecordingAlerter) -> None:
    await Watchtower(recorded).send_failed(
        EvolutionRateLimitedError("send a message", status=463, detail="reach-out time-lock")
    )

    (alert,) = recorded.messages
    assert Signal.RATE_LIMITED in alert
    assert "463" in alert


async def test_a_send_failure_that_is_not_a_rate_limit_still_reaches_the_maintainer(
    recorded: RecordingAlerter,
) -> None:
    await Watchtower(recorded).send_failed(EvolutionError("send a message", status=500))

    (alert,) = recorded.messages
    assert Signal.SEND_FAILED in alert
    assert Signal.RATE_LIMITED not in alert


async def test_a_temporary_ban_and_a_permanent_ban_read_differently(
    recorded: RecordingAlerter,
) -> None:
    """One says wait it out, the other says swap to the backup. Confusing them
    either burns the only warm standby or leaves Rebe dead for good."""
    tower = Watchtower(recorded)

    await tower.connection_changed("close", reason=401)
    await tower.connection_changed("close", reason=403)

    temporary, permanent = recorded.messages
    assert Signal.TEMPORARY_BAN in temporary
    assert Signal.PERMANENT_BAN in permanent
    assert "bien-backup" in permanent
    assert "bien-backup" not in temporary


async def test_a_ban_stops_the_sending_and_waits_for_a_human(
    recorded: RecordingAlerter, switch: InMemoryPauseSwitch
) -> None:
    """Section 5 of the deployment spec: stop, alert, wait. The switch is the stop."""
    await Watchtower(recorded, pause=switch).connection_changed("close", reason=403)

    state = await switch.state()
    assert state.paused is True
    assert Signal.PERMANENT_BAN in state.reason
    assert "paused" in recorded.messages[0]


async def test_a_disconnect_is_alerted_but_does_not_pause_her(
    recorded: RecordingAlerter, switch: InMemoryPauseSwitch
) -> None:
    """Evolution reconnects on its own, and a pause nobody undoes is worse than
    a few minutes of failed sends."""
    await Watchtower(recorded, pause=switch).connection_changed("close")

    assert Signal.DISCONNECTED in recorded.messages[0]
    assert (await switch.state()).paused is False


async def test_the_link_coming_up_is_not_an_alert(recorded: RecordingAlerter) -> None:
    await Watchtower(recorded).connection_changed("open")

    assert recorded.messages == []


async def test_a_deepseek_error_reaches_the_maintainer(recorded: RecordingAlerter) -> None:
    await Watchtower(recorded).brain_failed(RuntimeError("news_summary call failed: 500"))

    (alert,) = recorded.messages
    assert Signal.BRAIN_ERROR in alert
    assert "news_summary" in alert


async def test_a_ban_still_alerts_when_the_switch_cannot_be_flipped(
    recorded: RecordingAlerter,
) -> None:
    """A database that is down must not swallow the worst news the bot has."""

    class BrokenSwitch:
        async def state(self) -> None: ...

        async def set_paused(self, paused: bool, *, reason: str = "") -> None:
            raise RuntimeError("the rebe database is unreachable")

    await Watchtower(recorded, pause=BrokenSwitch()).connection_changed(  # type: ignore[arg-type]
        "close", reason=403
    )

    assert Signal.PERMANENT_BAN in recorded.messages[0]
