"""The ops channel: one Telegram chat that carries alerts out and control back in.

Section 2.4 of the deployment spec and the admin controls in
`docs/wayfinder/consent-group-rules-spec.md` describe the same channel from two
ends. Out of it come the alerts (`rebe_agent.alerts`) and the heartbeat
(`rebe_agent.heartbeat`). Back in comes exactly one thing: the soft pause.

**There is deliberately no in-group command.** No "Rebe pausa", nothing an
operator types in the chat, because members would see it and infer a bot - which
is the one thing the consent spec spends its whole length protecting. So the
control is a Telegram message from the one chat `TELEGRAM_CHAT_ID` names, and a
command from anywhere else gets silence rather than a confirmation that the bot
is there. The hard stop is not here either: an admin removes Rebe's number from
the group like any departing member, and to everyone else it looks like a person
left.

The listener is a long poll rather than a webhook, because the agent has no
public URL for Telegram to call (section 2.2) and a poll needs nothing exposed.

Both loops here are written to outlive whatever they are watching. A Telegram
outage or a database error is logged and retried, never raised, because the ops
channel is the last thing that should die when something breaks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

import httpx

from rebe_agent.alerts import Alerter, TelegramAlerter, ThrottledAlerter
from rebe_agent.clock import Clock
from rebe_agent.config import Settings
from rebe_agent.heartbeat import Heartbeat, build_heartbeat
from rebe_agent.pause import PauseState, PauseSwitch
from rebe_agent.signals import Watchtower
from rebe_agent.telegram import POLL_SECONDS, TelegramClient, TelegramError, Update, build_telegram

logger = logging.getLogger("rebe_agent.ops")

RETRY_SECONDS = 15.0
"""How long the control channel waits after a failed poll before trying again."""

SHUTDOWN_SECONDS = 5.0
"""How long a stopping loop is given to notice before it is cancelled outright."""

PAUSE_COMMANDS = frozenset({"pausa", "pause"})
RESUME_COMMANDS = frozenset({"reanuda", "resume"})
STATE_COMMANDS = frozenset({"estado", "status"})

HELP = (
    "Comandos: /pausa [motivo] para que Rebe se calle, /reanuda para que vuelva, "
    "/estado para ver como esta."
)

BY_HAND = "flipped by hand from the ops channel"
"""The reason recorded for a `/pausa` that did not give one."""


@dataclass(frozen=True, slots=True)
class Command:
    """A control message, split into the verb and whatever followed it."""

    verb: str
    rest: str


def parse(text: str) -> Command | None:
    """The command in `text`, or `None` if it is not one.

    Telegram appends `@thebot` to commands in group and some desktop clients, and
    a maintainer types whichever of the two words comes to hand, so both the
    suffix and the case are thrown away here.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped[1:].partition(" ")
    verb = head.split("@", 1)[0].casefold()
    return Command(verb=verb, rest=rest.strip()) if verb else None


class Control:
    """The way in: Telegram messages from the ops chat, and nothing else.

    Telegram redelivers an update until an offset past it is acknowledged, and the
    offset is advanced only *after* the command has been carried out. So a
    `/pausa` that arrived while the process was restarting still lands, and one
    whose write failed - the `rebe` database being briefly unreachable is the
    realistic way - is offered again on the next poll instead of being dropped by
    the one control path that exists to stop a banned number sending.

    That makes delivery at-least-once, which is safe because both verbs are
    idempotent: pausing an already-paused Rebe keeps the moment she went quiet.
    The operator's confirmation is what says it landed - no reply means it did not.
    """

    def __init__(
        self,
        telegram: TelegramClient,
        pause: PauseSwitch,
        chat_id: str,
        *,
        poll_seconds: int = POLL_SECONDS,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        self._telegram = telegram
        self._pause = pause
        self._chat_id = chat_id
        self._poll_seconds = poll_seconds
        self._retry_seconds = retry_seconds
        self._offset = 0

    async def run(self, stopping: asyncio.Event) -> None:
        """Poll for commands until `stopping` is set, then return."""
        logger.info("the ops control channel is listening")
        while not stopping.is_set():
            waiting = 0.0
            try:
                for update in await self._telegram.poll(
                    offset=self._offset, timeout=self._poll_seconds
                ):
                    await self.handle(update)
                    self._offset = max(self._offset, update.update_id + 1)
            except TelegramError as exc:
                logger.warning("could not read the ops channel: %s", exc)
                waiting = self._retry_seconds
            except Exception as exc:
                # Anything else - the `rebe` database refusing the write is the
                # realistic one - is a bad minute, not the end of the channel.
                logger.error("the ops channel could not handle an update: %s", exc)
                waiting = self._retry_seconds
            await self._breathe(stopping, waiting)
        logger.info("the ops control channel stopped")

    async def _breathe(self, stopping: asyncio.Event, seconds: float) -> None:
        """Hand the loop back, and wait `seconds` unless the process is stopping.

        The yield is unconditional: a poll that answers instantly - Telegram
        ignoring the long-poll timeout, or a proxy in front of it - must not turn
        this into a loop that never lets the heartbeat run.
        """
        await asyncio.sleep(0)
        if seconds <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), seconds)

    async def handle(self, update: Update) -> None:
        """Act on one message, if it came from the maintainer and means something."""
        if update.chat_id != self._chat_id:
            # No answer at all: a stranger who found the bot learns nothing.
            logger.warning("ignored a message from chat %s", update.chat_id)
            return

        command = parse(update.text)
        if command is None:
            return
        if command.verb in PAUSE_COMMANDS:
            await self._announce(await self._pause.set_paused(True, reason=command.rest or BY_HAND))
        elif command.verb in RESUME_COMMANDS:
            await self._announce(await self._pause.set_paused(False))
        elif command.verb in STATE_COMMANDS:
            await self._announce(await self._pause.state())
        else:
            await self._say(HELP)

    async def _announce(self, state: PauseState) -> None:
        """Confirm where the switch stands, so a flip is never taken on trust."""
        since = f" desde {state.since:%H:%M}" if state.since is not None else ""
        if state.paused:
            reason = f" ({state.reason})" if state.reason else ""
            await self._say(f"Rebe esta en pausa{since}{reason}. No va a enviar nada.")
        else:
            await self._say(f"Rebe esta activa{since}. Envia normal, dentro del envelope.")

    async def _say(self, text: str) -> None:
        """Answer in the ops chat. A failure here is worth a log line and no more."""
        try:
            await self._telegram.send_message(text)
        except TelegramError as exc:
            logger.warning("could not answer in the ops channel: %s", exc)


@dataclass(frozen=True, slots=True)
class OpsChannel:
    """Everything out of band, assembled: alerts, the heartbeat, and the switch."""

    alerts: Alerter
    watchtower: Watchtower
    pause: PauseSwitch
    heartbeat: Heartbeat
    control: Control

    async def serve(self, stopping: asyncio.Event, *, grace: float = SHUTDOWN_SECONDS) -> None:
        """Run the heartbeat and the control channel until `stopping` is set.

        Both, together, in one call: a deployment with alerts but no way to
        silence her, or a switch nobody can hear about, is half an ops channel.

        A stop is not an invitation to finish what you were doing. The control
        channel can be sitting in a twenty-five second long poll, and a container
        that took that long to exit gets killed rather than stopped - so the loops
        get a short grace period and are then cancelled. Whichever loop ends first
        ends the other: a heartbeat that has stopped beating is already an alert,
        and a process that keeps running without one is a process lying to Kuma.
        """
        loops = [
            asyncio.create_task(self.heartbeat.run(stopping), name="heartbeat"),
            asyncio.create_task(self.control.run(stopping), name="control"),
        ]
        halt = asyncio.create_task(stopping.wait(), name="stopping")
        try:
            await asyncio.wait([*loops, halt], return_when=asyncio.FIRST_COMPLETED)
            stopping.set()
            await asyncio.wait(loops, timeout=grace)
        finally:
            for task in (*loops, halt):
                task.cancel()
            await asyncio.gather(*loops, halt, return_exceptions=True)
        for loop in loops:
            failure = None if loop.cancelled() else loop.exception()
            if failure is not None:
                logger.error("the %s loop stopped: %s", loop.get_name(), failure)


def build_ops(
    settings: Settings,
    clock: Clock,
    pause: PauseSwitch,
    http_client: httpx.AsyncClient,
) -> OpsChannel:
    """Wire the ops channel from configuration.

    The pause switch comes in from outside rather than being built here: it is a
    table in the `rebe` database, and which pool that lives in is the caller's
    decision - every command shares one pool.
    """
    telegram = build_telegram(settings, http_client=http_client)
    alerts = ThrottledAlerter(TelegramAlerter(telegram), clock)
    return OpsChannel(
        alerts=alerts,
        watchtower=Watchtower(alerts, pause=pause),
        pause=pause,
        heartbeat=build_heartbeat(settings, http_client),
        control=Control(telegram, pause, settings.telegram_chat_id),
    )
