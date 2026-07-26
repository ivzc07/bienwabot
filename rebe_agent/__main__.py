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
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from types import FrameType

import httpx

from rebe_agent import __version__
from rebe_agent.brain import BrainError, Probe, build_brain
from rebe_agent.clock import Clock, SystemClock
from rebe_agent.config import ConfigurationError, Settings, load_settings
from rebe_agent.curate import DEFAULT_FILTERS
from rebe_agent.db import Pool, open_pool
from rebe_agent.evolution import EvolutionError, EvolutionSender, build_client
from rebe_agent.feeds import WebCandidates
from rebe_agent.news import NewsLeg
from rebe_agent.pacer import Pacer, SendRefusedError
from rebe_agent.plans import PostgresPlanStore
from rebe_agent.posted import PostgresPostedStore
from rebe_agent.scheduler import Scheduler
from rebe_agent.sends import PostgresSendLog, SendKind
from rebe_agent.usage import CallType, PostgresUsageStore

EXIT_OK = 0
EXIT_BAD_CONFIG = 2
EXIT_CALL_FAILED = 3
EXIT_SEND_REFUSED = 4
EXIT_SEND_FAILED = 5

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


async def ask_once(settings: Settings, clock: Clock, prompt: str) -> int:
    """One prompt through the real brain, printed as a validated typed object.

    This is the smallest end-to-end proof that the wiring works: the model ID,
    the disabled thinking mode, the token cap, the schema, the counter, and the
    `rebe` database all take part in one command.
    """
    async with PostgresUsageStore.connect(settings.rebe_database_url.get_secret_value()) as store:
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
        PostgresSendLog.connect(settings.rebe_database_url.get_secret_value()) as log,
        build_client(settings) as client,
    ):
        pacer = Pacer(client, log, clock)
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
    """The news leg and the two stores the scheduler leg reads alongside it.

    Every store lives in the same `rebe` database, so they share one pool rather
    than opening four.
    """

    leg: NewsLeg
    sends: PostgresSendLog
    plans: PostgresPlanStore


async def build_news_stack(
    settings: Settings,
    clock: Clock,
    pool: Pool,
    evolution: EvolutionSender,
    web: httpx.AsyncClient,
) -> NewsStack:
    """Wire the whole news path against one pool, tables included."""
    usage = PostgresUsageStore(pool)
    sends = PostgresSendLog(pool)
    posted = PostgresPostedStore(pool)
    plans = PostgresPlanStore(pool, settings.zone)
    await usage.ensure_schema()
    await sends.ensure_schema()
    await posted.ensure_schema()
    await plans.ensure_schema()

    # One `Filters` for both halves: the fetch asks each source for exactly
    # what the curator would have kept, rather than for its own idea of it.
    leg = NewsLeg(
        build_brain(settings, clock, usage),
        Pacer(evolution, sends, clock),
        WebCandidates(web, filters=DEFAULT_FILTERS),
        posted,
        clock,
        filters=DEFAULT_FILTERS,
    )
    return NewsStack(leg=leg, sends=sends, plans=plans)


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
    ):
        stack = await build_news_stack(settings, clock, pool, evolution, web)
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


async def serve_the_scheduler(settings: Settings, clock: Clock) -> int:
    """The scheduler leg, running until the task is cancelled.

    One long-running loop in this one process, per the single-replica invariant in
    section 2.2 of the deployment spec. The webhook leg lands in a later ticket and
    joins this process rather than a second one, because the pacer's ceilings span
    both legs and a limiter cannot span two processes.
    """
    async with (
        open_pool(settings.rebe_database_url.get_secret_value()) as pool,
        build_client(settings) as evolution,
        httpx.AsyncClient() as web,
    ):
        stack = await build_news_stack(settings, clock, pool, evolution, web)
        scheduler = Scheduler(stack.leg, settings.rebe_group_jid, stack.plans, stack.sends, clock)
        logger.info("posting into %s", settings.rebe_group_jid)
        await scheduler.serve()
    return EXIT_OK


def stop_on_signals(stopping: asyncio.Event) -> None:
    """Turn SIGTERM and SIGINT into something the event loop can wait on.

    `signal.signal` rather than the loop's own handler because that one is
    POSIX-only, and the tests run on Windows.
    """
    loop = asyncio.get_running_loop()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("received %s, shutting down", signal.Signals(signum).name)
        loop.call_soon_threadsafe(stopping.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)


async def serve(settings: Settings, clock: Clock) -> int:
    """Run the legs until the platform stops the process.

    A leg that ends on its own has broken, and its exception is deliberately let
    out: the platform restarts the container, and a restart is safe precisely
    because the day's plan is in the database rather than in this process.
    """
    stopping = asyncio.Event()
    stop_on_signals(stopping)

    serving = asyncio.create_task(serve_the_scheduler(settings, clock), name="scheduler")
    halt = asyncio.create_task(stopping.wait(), name="stop")
    done, _ = await asyncio.wait({serving, halt}, return_when=asyncio.FIRST_COMPLETED)

    if serving in done:
        halt.cancel()
        return serving.result()

    serving.cancel()
    with suppress(asyncio.CancelledError):
        await serving
    logger.info("rebe-agent stopped")
    return EXIT_OK


def run(settings: Settings, clock: Clock) -> int:
    """Boot the bot that runs itself: the dawn roll, and the day it draws."""
    apply_timezone(settings.timezone)
    return asyncio.run(serve(settings, clock))


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

    return run(settings, clock)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
