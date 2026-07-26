"""Boot behaviour: what the process logs, and how it refuses to start."""

from __future__ import annotations

import logging

import pytest

from rebe_agent.__main__ import main
from tests.test_config import COMPLETE_ENV


def test_a_complete_environment_boots_and_names_the_active_instance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = dict(COMPLETE_ENV, EVOLUTION_INSTANCE="bien-backup")

    with caplog.at_level(logging.INFO):
        exit_code = main(["--check-config"], env=env)

    assert exit_code == 0
    startup = "\n".join(record.getMessage() for record in caplog.records)
    assert "bien-backup" in startup
    assert "America/Mexico_City" in startup


def test_the_startup_line_never_prints_a_secret(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        main(["--check-config"], env=dict(COMPLETE_ENV))

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("sk-deepseek-test", "evo-key-test", "webhook-secret-test"):
        assert secret not in logged


def test_a_missing_variable_exits_non_zero_and_names_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = dict(COMPLETE_ENV)
    del env["EVOLUTION_API_KEY"]

    with caplog.at_level(logging.ERROR):
        exit_code = main(["--check-config"], env=env)

    assert exit_code != 0
    assert "EVOLUTION_API_KEY" in "\n".join(record.getMessage() for record in caplog.records)


def test_posting_news_needs_somewhere_to_post_it() -> None:
    """Caught by argparse, before configuration is even read: a run that would
    fetch, rank and summarise and then have nowhere to send is wasted money."""
    with pytest.raises(SystemExit):
        main(["--post-news"], env=dict(COMPLETE_ENV))


def test_the_per_run_cap_must_be_a_number_of_posts() -> None:
    with pytest.raises(SystemExit):
        main(["--post-news", "--to", "1203@g.us", "--limit", "0"], env=dict(COMPLETE_ENV))


def test_an_invalid_variable_exits_non_zero_and_explains(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = dict(COMPLETE_ENV, TZ="Mars/Olympus_Mons")

    with caplog.at_level(logging.ERROR):
        exit_code = main(["--check-config"], env=env)

    assert exit_code != 0
    assert "TZ" in "\n".join(record.getMessage() for record in caplog.records)
