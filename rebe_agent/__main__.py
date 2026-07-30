"""Boot: load configuration, prove it, then hand off to the run loop.

Exit codes: 0 clean stop, 2 unusable configuration, 3 the brain gave no answer,
4 the pacer refused the send, 5 Evolution would not take it. The last two are
separate on purpose: "not now" and "broken" want different reactions from
whoever is watching.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from functools import partial

import httpx
import psycopg
import uvicorn
from psycopg_pool import PoolTimeout

from rebe_agent import __version__
from rebe_agent.announce import Announcer
from rebe_agent.brain import BrainError, Probe, build_brain
from rebe_agent.breaking import Breaking
from rebe_agent.cadence import Cadence, dense_cadence
from rebe_agent.chimeins import ChimeInBudget, PostgresChimeInLog
from rebe_agent.clock import Clock, SystemClock
from rebe_agent.config import ConfigurationError, Settings, load_settings
from rebe_agent.curate import DEFAULT_FILTERS
from rebe_agent.db import Pool, open_pool
from rebe_agent.evolution import EvolutionError, EvolutionSender, build_client
from rebe_agent.feeds import WebCandidates
from rebe_agent.memory import PostgresGroupMemory
from rebe_agent.news import NewsLeg
from rebe_agent.ops import OpsChannel, build_ops
from rebe_agent.overnight import PostgresOvernightQueue
from rebe_agent.pacer import Envelope, Pacer, SendRefusedError, dense_envelope
from rebe_agent.pause import Pause, PostgresPauseSwitch
from rebe_agent.plans import PostgresPlanStore
from rebe_agent.posted import PostgresPostedStore
from rebe_agent.preview import preview_image_url
from rebe_agent.ramp import PostgresRampStore, Ramp
from rebe_agent.reply import ReplyLeg
from rebe_agent.scheduler import Scheduler
from rebe_agent.sends import PostgresSendLog, SendKind
from rebe_agent.usage import CallType, PostgresUsageStore
from rebe_agent.webhook import WEBHOOK_HOST, WEBHOOK_PORT, build_app

EXIT_OK = 0
EXIT_BAD_CONFIG = 2
EXIT_CALL_FAILED = 3
EXIT_SEND_REFUSED = 4
EXIT_SEND_FAILED = 5

SWITCH_READY_SECONDS = 5.0
"""How long boot spends reaching the soft-pause switch before carrying on without it."""

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

logger = logging.getLogger("rebe_agent")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rebe-agent", description="The bien.mx news agent.")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate the environment, log the startup line, and exit without serving.",
    )
    parser.add_argument(
        "--ask",
        metavar="PROMPT",
        help=(
            "Send one prompt through the DeepSeek brain, print the validated answer "
            "as JSON, and exit. The call is counted and its tokens recorded like any other."
        ),
    )
    parser.add_argument(
        "--say",
        metavar="TEXT",
        help=(
            "Send one message through the shared pacer and exit. Requires --to. "
            "Typing presence, the jittered delay and every ceiling apply, so this "
            "is the real send path rather than a shortcut around it."
        ),
    )
    parser.add_argument(
        "--to",
        metavar="JID",
        help="Where --say sends: a WhatsApp group or contact JID, for example 1203...@g.us.",
    )
    parser.add_argument(
        "--as",
        dest="kind",
        choices=[str(kind) for kind in SendKind],
        default=str(SendKind.POST),
        help=(
            "Which leg --say pretends to be. Posts are held overnight and spaced "
            "75-90 minutes apart; replies are not. Default: post."
        ),
    )
    parser.add_argument(
        "--post-news",
        action="store_true",
        help=(
            "Run the news leg once: fetch, curate, summarise, and post the best "
            "unposted item through the pacer. Requires --to. Nothing to post is "
            "a clean exit, not a failure."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        metavar="N",
        help="The per-run cap on how many items --post-news may post. Default: 1.",
    )
    args = parser.parse_args(argv)
    if args.say and not args.to:
        parser.error("--say needs --to <JID> to know where to send")
    if args.post_news and not args.to:
        parser.error("--post-news needs --to <JID> to know where to post")
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def _configure_logging(level: int | str = logging.INFO) -> None:
    logging.basicConfig(format=LOG_FORMAT, level=level)
    logging.getLogger().setLevel(level)
    # httpx logs the URL of every request it makes at INFO, and Telegram puts the
    # bot token in the URL *path* - so an INFO-level run would write that
    # credential to the log every time the ops channel polls. Nothing here needs
    # that line: each call site already logs what it did and what came back.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def apply_timezone(name: str) -> None:
    """Make the process's local time match the configured zone.

    A `Clock` already carries the zone everywhere the agent reasons about time;
    this aligns anything that reads local time the POSIX way (log timestamps,
    libraries calling `localtime`). `tzset` is POSIX-only, so it is skipped
    elsewhere.

    Process-global by nature, so only the serving path calls it: `--check-config`
    and the tests leave the ambient environment alone.
    """
    os.environ["TZ"] = name
    tzset = getattr(time, "tzset", None)
    if tzset is not None:
        tzset()


def log_startup(settings: Settings, clock: Clock) -> None:
    """The one line an operator looks for to know which number is live."""
    logger.info(
        "rebe-agent %s starting | evolution_instance=%s evolution_api=%s timezone=%s local_time=%s",
        __version__,
        settings.evolution_instance,
        settings.evolution_api_url,
        settings.timezone,
        clock.now().isoformat(timespec="seconds"),
    )


@asynccontextmanager
async def open_ops(settings: Settings, clock: Clock, pool: Pool) -> AsyncIterator[OpsChannel]:
    """The ops channel, over its own HTTP client. The one place it is assembled.

    Every command that can send opens this, and so does the run loop: the soft
    pause has to gate a `--say` and a `--post-news` exactly as it gates the loop,
    or "Rebe goes silent" would be a promise about one code path out of three.
    The post-pairing ramp is the same shape of promise and comes out of the same
    place, so callers take both off the channel rather than building either.

    Neither table is created here. Both are made on first use, so a database that
    is briefly unreachable delays the switch and the ramp instead of stopping the
    boot that was going to alert about it.
    """
    async with httpx.AsyncClient() as http:
        yield build_ops(
            settings,
            clock,
            PostgresPauseSwitch(pool, clock),
            http,
            Ramp(PostgresRampStore(pool), clock, PostgresSendLog(pool)),
        )


async def ask_once(settings: Settings, clock: Clock, prompt: str) -> int:
    """One prompt through the real brain, printed as a validated typed object.

    This is the smallest end-to-end proof that the wiring works: the model ID,
    the disabled thinking mode, the token cap, the schema, the counter, and the
    `rebe` database all take part in one command.
    """
    async with PostgresUsageStore.connect(settings.rebe_database_url.get_secret_value()) as store:
        # No ops channel here on purpose: this is a smoke test somebody is
        # watching run, and a failed probe is already on their screen. Waking the
        # maintainer over Telegram for it would be an alert about nothing.
        brain = build_brain(settings, clock, store)
        try:
            answer = await brain.ask(CallType.PROBE, prompt, Probe)
        except BrainError as exc:
            logger.error("the brain returned no answer. %s", exc)
            return EXIT_CALL_FAILED

        print(answer.model_dump_json(indent=2))

        day = clock.now().date()
        totals = (await store.totals_on(day)).get(CallType.PROBE)
        if totals is not None:
            logger.info(
                "usage on %s for %s: calls=%d cache_hit=%d cache_miss=%d completion=%d",
                day.isoformat(),
                CallType.PROBE,
                totals.calls,
                totals.cache_hit_tokens,
                totals.cache_miss_tokens,
                totals.completion_tokens,
            )
    return EXIT_OK


async def say_once(settings: Settings, clock: Clock, chat: str, text: str, kind: SendKind) -> int:
    """One message into a real group, through the real pacer.

    The smallest end-to-end proof that the send path works: the group sees the
    typing indicator, the pause is drawn rather than fixed, the send is written
    to the `rebe` database before it goes on the wire, and the envelope gets its
    say. A refusal and a transport failure exit differently, because "come back
    in forty minutes" and "Evolution is down" are not the same news.
    """
    async with (
        open_pool(settings.rebe_database_url.get_secret_value()) as pool,
        build_client(settings) as client,
        open_ops(settings, clock, pool) as ops,
    ):
        log = PostgresSendLog(pool)
        await log.ensure_schema()
        _, envelope = posting_posture(settings)
        pacer = Pacer(
            client,
            log,
            clock,
            envelope=envelope,
            pause=ops.pause,
            watch=ops.watchtower,
            ramp=ops.ramp,
        )
        try:
            # The pacer logs the send it made; there is one event here, not two.
            await pacer.send(kind, chat, text)
        except SendRefusedError as exc:
            logger.error("the pacer refused the send. %s", exc)
            return EXIT_SEND_REFUSED
        except EvolutionError as exc:
            logger.error("the message did not get out. %s", exc)
            return EXIT_SEND_FAILED

    return EXIT_OK


@dataclass(frozen=True, slots=True)
class NewsStack:
    """The news leg, the pacer it sends through, and the stores around them.

    Every store lives in the same `rebe` database, so they share one pool rather
    than opening four. The pacer is held here rather than hidden inside the leg
    because the reply leg has to be handed *this* one: two pacers would each stay
    politely under twelve sends a day and between them send twenty-four.
    """

    leg: NewsLeg
    pacer: Pacer
    usage: PostgresUsageStore
    sends: PostgresSendLog
    posted: PostgresPostedStore
    plans: PostgresPlanStore
    overnight: PostgresOvernightQueue

    async def ensure_schema(self) -> None:
        """Make sure every table this stack writes to exists."""
        await self.usage.ensure_schema()
        await self.sends.ensure_schema()
        await self.posted.ensure_schema()
        await self.plans.ensure_schema()
        await self.overnight.ensure_schema()


def posting_posture(settings: Settings) -> tuple[Cadence, Envelope]:
    """The day's shape and the envelope that has to let that shape through.

    Defaults are the shipped human cadence. `CADENCE_SLOT_MINUTES` swaps in the
    dense experiment and shrinks the post-to-post gap to match - otherwise the
    scheduler would draw a slot every half hour and the pacer would refuse it.
    """
    minutes = settings.cadence_slot_minutes
    if minutes is None:
        return Cadence(), Envelope()
    logger.warning(
        "dense cadence on: every %dm from 08:00-23:00 - WhatsApp may treat this as automation",
        minutes,
    )
    return dense_cadence(minutes), dense_envelope(minutes)


def build_news_stack(
    settings: Settings,
    clock: Clock,
    pool: Pool,
    evolution: EvolutionSender,
    web: httpx.AsyncClient,
    ops: OpsChannel,
) -> NewsStack:
    """Wire the whole news path against one pool.

    Wiring only: the tables are prepared by whoever calls this, because a one-shot
    command wants to fail loudly on a cold database and the long-running process
    must come up regardless and let the ops channel say so.

    The ops channel is threaded through rather than reached for later. The pacer
    holds the soft pause, the ramp and the watchtower, and the brain holds the
    alerter, so a send that is refused is silent and a send that breaks is heard
    about.
    """
    usage = PostgresUsageStore(pool)
    sends = PostgresSendLog(pool)
    posted = PostgresPostedStore(pool)
    plans = PostgresPlanStore(pool, settings.zone)
    overnight = PostgresOvernightQueue(pool)
    _, envelope = posting_posture(settings)
    pacer = Pacer(
        evolution,
        sends,
        clock,
        envelope=envelope,
        pause=ops.pause,
        watch=ops.watchtower,
        ramp=ops.ramp,
    )

    brain = build_brain(settings, clock, usage, ops.alerts)
    # The same brain and the same pacer as the posts the twins follow: the
    # announcement leg exists only when the operator names a channel for it.
    announcer = (
        Announcer(brain, pacer, settings.rebe_announce_jid)
        if settings.rebe_announce_jid is not None
        else None
    )
    # One `Filters` for both halves: the fetch asks each source for exactly
    # what the curator would have kept, rather than for its own idea of it.
    leg = NewsLeg(
        brain,
        pacer,
        WebCandidates(web, filters=DEFAULT_FILTERS),
        posted,
        clock,
        filters=DEFAULT_FILTERS,
        # The same web client the feeds use: the lookup is one more bounded GET,
        # and a second client would be a second set of connections to tune.
        preview=partial(preview_image_url, web),
        announcer=announcer,
    )
    return NewsStack(
        leg=leg,
        pacer=pacer,
        usage=usage,
        sends=sends,
        posted=posted,
        plans=plans,
        overnight=overnight,
    )


async def post_news_once(settings: Settings, clock: Clock, chat: str, limit: int) -> int:
    """One turn of the news leg: the open web to the group, once, on demand.

    The same leg the scheduler fires on a drawn time, run by hand: this is how an
    operator proves the path end to end without waiting for a window to open.

    Posting nothing exits cleanly. On a healthy day the second run in a row has
    nothing left to say, and an operator should not have to read that as a fault.
    """
    async with (
        open_pool(settings.rebe_database_url.get_secret_value()) as pool,
        build_client(settings) as evolution,
        httpx.AsyncClient() as web,
        open_ops(settings, clock, pool) as ops,
    ):
        stack = build_news_stack(settings, clock, pool, evolution, web, ops)
        # A command run by hand should fail on a cold database rather than post
        # nothing and exit clean, so this one waits for the tables.
        await stack.ensure_schema()
        try:
            sent = await stack.leg.run(chat, limit=limit)
        except SendRefusedError as exc:
            logger.error("the pacer refused the post. %s", exc)
            return EXIT_SEND_REFUSED
        except EvolutionError as exc:
            logger.error("the post did not get out. %s", exc)
            return EXIT_SEND_FAILED

    for post in sent:
        logger.info("posted %s (%s)", post.item.canonical_url, post.item.source)
    if not sent:
        logger.info("nothing new was worth posting")
    return EXIT_OK


def stop_on_signals(stopping: asyncio.Event) -> None:
    """Ask the loops to finish on SIGTERM or SIGINT.

    `add_signal_handler` is the asyncio-safe way and is POSIX-only, which is where
    the container runs. The fallback keeps a local run on Windows interruptible,
    and hands the event back to the loop's thread rather than setting it inside
    the handler.
    """
    loop = asyncio.get_running_loop()

    def stop(name: str) -> None:
        logger.info("received %s, shutting down", name)
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop, sig.name)
        except NotImplementedError:  # pragma: no cover - Windows only
            signal.signal(
                sig,
                lambda number, _frame: loop.call_soon_threadsafe(stop, signal.Signals(number).name),
            )


async def log_the_soft_pause(pause: Pause) -> None:
    """Log where the switch stands at boot, or say why it could not be read.

    Bounded and forgiving on purpose. A pause survives a restart, so whether this
    process starts out silent is the one line an operator needs. But the heartbeat
    and the control channel are exactly what a maintainer wants alive when the
    `rebe` database is unreachable, and a boot that waited here would take them
    down with it - and with them any chance of hearing about it.
    """
    try:
        state = await asyncio.wait_for(pause.state(), SWITCH_READY_SECONDS)
    except (TimeoutError, psycopg.Error, PoolTimeout) as exc:
        logger.error(
            "could not read the soft pause switch (%s): %s. The ops channel is "
            "coming up anyway, so somebody can hear about it.",
            type(exc).__name__,
            str(exc) or "no detail",
        )
        return
    if state.paused:
        logger.warning(
            "starting PAUSED since %s (%s): nothing will be sent until an operator "
            "resumes her from the ops chat",
            state.since.isoformat(timespec="seconds") if state.since else "unknown",
            state.reason or "no reason recorded",
        )
    else:
        logger.info("the soft pause is off; sending is normal")


async def log_the_ramp(ramp: Ramp) -> None:
    """Log where the post-pairing ramp stands at boot, and start it if it is new.

    Bounded and forgiving in exactly the way `log_the_soft_pause` is, and for the
    same reason: an unreachable `rebe` database must not take down the heartbeat
    and the control channel that are how anybody hears about it.

    This is also the moment a fresh deployment's ramp is stamped. The agent has
    no pairing event to hang it on, so first boot is the closest honest thing
    there is - and having it happen here means the log says when.
    """
    try:
        state = await asyncio.wait_for(ramp.state(), SWITCH_READY_SECONDS)
        cap = await asyncio.wait_for(ramp.post_cap(), SWITCH_READY_SECONDS)
    except (TimeoutError, psycopg.Error, PoolTimeout) as exc:
        logger.error(
            "could not read the post-pairing ramp (%s): %s. Rebe will be clamped "
            "or not according to whatever the next read finds.",
            type(exc).__name__,
            str(exc) or "no detail",
        )
        return
    logger.info(
        "the ramp started %s (%s); today allows %s",
        state.started_at.isoformat(timespec="seconds"),
        state.reason,
        f"{cap} news posts" if cap is not None else "the cadence spec's steady state",
    )


class EmbeddedServer(uvicorn.Server):
    """Uvicorn with its signal handling left to the caller.

    Uvicorn installs its own SIGTERM and SIGINT handlers over ours the moment it
    starts serving, and they stop the server alone. That would leave the ops
    channel beating away in a process that no longer answers a webhook, which is
    the one shape of shutdown a maintainer must never see: Kuma stays green while
    Rebe has gone deaf. One `stopping` event owns the whole process instead.
    """

    def install_signal_handlers(self) -> None:
        return None


async def serve(settings: Settings, clock: Clock) -> int:
    """Hold the process open: the webhook leg and the ops channel, together.

    One pool and one pacer across everything that sends. The pacer is built here,
    with the soft pause and the watchtower already on it, because a limiter that
    only some callers hold is not a limiter - and a pause an operator flips has to
    silence replies as surely as it silences posts.

    The schema is prepared *beside* the server rather than before it. A Postgres
    that is briefly unreachable at boot must not stop the process coming up: an
    agent that is alive and silent recovers on its own, while one that crash-loops
    on a cold database needs a human - and the ops channel is exactly what carries
    the news that the database is cold.
    """
    apply_timezone(settings.timezone)
    stopping = asyncio.Event()
    # Before anything that can wait on the network: a SIGTERM in the first few
    # seconds must stop the process rather than kill it, and until this runs the
    # default action for SIGTERM is to die on the spot.
    stop_on_signals(stopping)

    async with (
        open_pool(settings.rebe_database_url.get_secret_value()) as pool,
        build_client(settings) as evolution,
        httpx.AsyncClient() as web,
        open_ops(settings, clock, pool) as ops,
    ):
        memory = PostgresGroupMemory(pool)
        chime_ins = PostgresChimeInLog(pool)
        stack = build_news_stack(settings, clock, pool, evolution, web, ops)
        preparing = asyncio.create_task(_prepare(stack, memory, chime_ins))

        await log_the_soft_pause(ops.pause)
        await log_the_ramp(ops.ramp)

        # `stack.pacer`, not a second one. Both legs send through the object that
        # holds the day's count, the turnstile and the soft pause, which is what
        # makes the ceilings span news posts and replies rather than double.
        # The chime-in budget is per local day and lives in the same database as
        # everything else, so a restart does not hand her a fresh allowance to
        # speak up uninvited.
        reply = ReplyLeg(
            build_brain(settings, clock, stack.usage, ops.alerts),
            stack.pacer,
            evolution,
            memory,
            ChimeInBudget(chime_ins, clock),
        )
        # The override leg posts through the same news leg and the same pacer the
        # drawn slots do, so "important news is never held back by the quota"
        # never becomes "important news is never held back".
        cadence, _ = posting_posture(settings)
        breaking = Breaking(
            stack.leg,
            settings.rebe_group_jid,
            stack.plans,
            stack.sends,
            stack.overnight,
            clock,
            cadence=cadence,
        )
        scheduler = Scheduler(
            stack.leg,
            settings.rebe_group_jid,
            stack.plans,
            stack.sends,
            clock,
            cadence=cadence,
            breaking=breaking,
        )
        server = EmbeddedServer(
            uvicorn.Config(
                build_app(
                    reply,
                    settings.webhook_secret.get_secret_value(),
                    # The other event each instance subscribes to. Without this
                    # the link going down would be an alert and nothing more,
                    # and a reconnect would resume at the previous rate.
                    link=ops.watchtower,
                ),
                host=WEBHOOK_HOST,
                port=WEBHOOK_PORT,
                # The agent configures its own logging from LOG_LEVEL; uvicorn's
                # defaults would replace it and take the format with them.
                log_config=None,
                access_log=False,
            )
        )
        logger.info(
            "serving the webhook leg on port %d and posting into %s as instance %s",
            WEBHOOK_PORT,
            settings.rebe_group_jid,
            settings.evolution_instance,
        )
        try:
            await asyncio.gather(
                _serve_webhook(server, stopping),
                _serve_scheduler(scheduler, stopping),
                ops.serve(stopping),
            )
        finally:
            preparing.cancel()

    logger.info("rebe-agent stopped")
    return EXIT_OK


async def _serve_scheduler(scheduler: Scheduler, stopping: asyncio.Event) -> None:
    """Run the dawn roll and the day it draws, until the process is asked to stop.

    `Scheduler.serve` runs until cancelled, so the stop is a cancellation. A
    scheduler that ended on its own has broken rather than finished, and taking
    the rest of the process down with it is the wanted behaviour: the platform
    restarts the container, and a restart is safe precisely because the day's
    plan lives in the database rather than in this process.
    """
    serving = asyncio.create_task(scheduler.serve(), name="scheduler")
    halt = asyncio.create_task(stopping.wait(), name="stopping")
    try:
        await asyncio.wait([serving, halt], return_when=asyncio.FIRST_COMPLETED)
        stopping.set()
        if serving.done():
            await serving
    finally:
        serving.cancel()
        halt.cancel()
        with suppress(asyncio.CancelledError):
            await serving


async def _serve_webhook(server: EmbeddedServer, stopping: asyncio.Event) -> None:
    """Serve until the process is asked to stop, and stop the process if it falls over.

    Both directions are wired on purpose. A signal has to reach uvicorn, which is
    otherwise deaf now that it installs no handlers of its own; and a server that
    died on its own has to bring the ops channel down with it, because an agent
    that cannot be reached is not an agent worth keeping a heartbeat for.
    """
    serving = asyncio.create_task(server.serve(), name="webhook")
    halt = asyncio.create_task(stopping.wait(), name="stopping")
    try:
        await asyncio.wait([serving, halt], return_when=asyncio.FIRST_COMPLETED)
        stopping.set()
        server.should_exit = True
        await serving
    finally:
        halt.cancel()


async def _prepare(
    stack: NewsStack, memory: PostgresGroupMemory, chime_ins: PostgresChimeInLog
) -> None:
    """Make sure the `rebe` tables exist, without being able to stop the boot."""
    try:
        await stack.ensure_schema()
        await memory.ensure_schema()
        await chime_ins.ensure_schema()
    except psycopg.Error as exc:
        logger.error("the rebe database is not ready; Rebe will stay silent until it is. %s", exc)
    else:
        logger.info("the rebe database is ready")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging()

    try:
        settings = load_settings(env)
    except ConfigurationError as exc:
        logger.error("rebe-agent cannot start. %s", exc)
        return EXIT_BAD_CONFIG

    _configure_logging(settings.log_level)

    clock = SystemClock(settings.zone)
    log_startup(settings, clock)

    if args.check_config:
        logger.info("configuration is complete and valid")
        return EXIT_OK

    if args.ask:
        return asyncio.run(ask_once(settings, clock, args.ask))

    if args.say:
        return asyncio.run(say_once(settings, clock, args.to, args.say, SendKind(args.kind)))

    if args.post_news:
        return asyncio.run(post_news_once(settings, clock, args.to, args.limit))

    return asyncio.run(serve(settings, clock))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
