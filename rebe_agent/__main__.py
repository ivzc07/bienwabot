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
from contextlib import asynccontextmanager

import httpx
import psycopg
from psycopg_pool import PoolTimeout

from rebe_agent import __version__
from rebe_agent.brain import BrainError, Probe, build_brain
from rebe_agent.clock import Clock, SystemClock
from rebe_agent.config import ConfigurationError, Settings, load_settings
from rebe_agent.curate import DEFAULT_FILTERS
from rebe_agent.db import Pool, open_pool
from rebe_agent.evolution import EvolutionError, build_client
from rebe_agent.feeds import WebCandidates
from rebe_agent.news import NewsLeg
from rebe_agent.ops import OpsChannel, build_ops
from rebe_agent.pacer import Pacer, SendRefusedError
from rebe_agent.pause import Pause, PostgresPauseSwitch
from rebe_agent.posted import PostgresPostedStore
from rebe_agent.sends import PostgresSendLog, SendKind
from rebe_agent.usage import CallType, PostgresUsageStore

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

    The switch's table is created on first use rather than here, so that a
    database that is briefly unreachable delays the switch instead of stopping the
    boot that was going to alert about it.
    """
    async with httpx.AsyncClient() as http:
        yield build_ops(settings, clock, PostgresPauseSwitch(pool, clock), http)


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
        pacer = Pacer(client, log, clock, pause=ops.pause, watch=ops.watchtower)
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


async def post_news_once(settings: Settings, clock: Clock, chat: str, limit: int) -> int:
    """One turn of the news leg: the open web to the group, once, on demand.

    Every store lives in the same `rebe` database, so they share one pool rather
    than opening three. When this runs is the cadence ticket's decision; this
    command is what that ticket will eventually be scheduling.

    Posting nothing exits cleanly. On a healthy day the second run in a row has
    nothing left to say, and an operator should not have to read that as a fault.
    """
    async with (
        open_pool(settings.rebe_database_url.get_secret_value()) as pool,
        build_client(settings) as evolution,
        httpx.AsyncClient() as web,
        open_ops(settings, clock, pool) as ops,
    ):
        usage = PostgresUsageStore(pool)
        sends = PostgresSendLog(pool)
        posted = PostgresPostedStore(pool)
        await usage.ensure_schema()
        await sends.ensure_schema()
        await posted.ensure_schema()

        # One `Filters` for both halves: the fetch asks each source for exactly
        # what the curator would have kept, rather than for its own idea of it.
        leg = NewsLeg(
            build_brain(settings, clock, usage, ops.alerts),
            Pacer(evolution, sends, clock, pause=ops.pause, watch=ops.watchtower),
            WebCandidates(web, filters=DEFAULT_FILTERS),
            posted,
            clock,
            filters=DEFAULT_FILTERS,
        )
        try:
            sent = await leg.run(chat, limit=limit)
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


async def serve(settings: Settings, clock: Clock) -> int:
    """Hold the process open, with the ops channel running, until it is stopped.

    The two legs are still on demand - `--post-news` now, the cadence and webhook
    tickets later - so what this loop carries today is the out-of-band channel:
    the Kuma heartbeat, and the Telegram listener that carries the soft pause.
    Both are the same shape as the legs will be, and the heartbeat proves this
    loop is turning, which is the whole reason it is emitted from in here.
    """
    apply_timezone(settings.timezone)
    stopping = asyncio.Event()
    # Before anything that can wait on the network: a SIGTERM in the first few
    # seconds must stop the process rather than kill it, and until this runs the
    # default action for SIGTERM is to die on the spot.
    stop_on_signals(stopping)

    async with (
        open_pool(settings.rebe_database_url.get_secret_value()) as pool,
        open_ops(settings, clock, pool) as ops,
    ):
        await log_the_soft_pause(ops.pause)
        logger.info(
            "no legs wired yet; the ops channel is up for instance %s",
            settings.evolution_instance,
        )
        await ops.serve(stopping)
    logger.info("rebe-agent stopped")
    return EXIT_OK


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
