"""Boot: load configuration, prove it, then hand off to the run loop.

Exit codes: 0 clean stop, 2 unusable configuration, 3 the brain gave no answer.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import threading
import time
from collections.abc import Mapping, Sequence
from types import FrameType

from rebe_agent import __version__
from rebe_agent.brain import BrainError, Probe, build_brain
from rebe_agent.clock import Clock, SystemClock
from rebe_agent.config import ConfigurationError, Settings, load_settings
from rebe_agent.usage import CallType, PostgresUsageStore

EXIT_OK = 0
EXIT_BAD_CONFIG = 2
EXIT_CALL_FAILED = 3

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
    return parser.parse_args(argv)


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


def run(settings: Settings, clock: Clock) -> int:
    """Hold the process open until the platform stops it.

    The webhook leg and the news leg land in later tickets; this skeleton only
    has to boot, stay up, and shut down cleanly on SIGTERM.
    """
    apply_timezone(settings.timezone)
    stopping = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("received %s, shutting down", signal.Signals(signum).name)
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)

    logger.info("no legs wired yet; idling as instance %s", settings.evolution_instance)
    stopping.wait()
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

    return run(settings, clock)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
