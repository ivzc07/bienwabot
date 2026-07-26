"""Configuration is the single entry point for every environment variable."""

from __future__ import annotations

from pathlib import Path

import pytest

from rebe_agent.config import (
    REQUIRED_VARIABLES,
    InvalidConfigurationError,
    MissingConfigurationError,
    Settings,
    load_settings,
)

COMPLETE_ENV = {
    "DEEPSEEK_API_KEY": "sk-deepseek-test",
    "EVOLUTION_API_URL": "http://bien-evo:8080",
    "EVOLUTION_API_KEY": "evo-key-test",
    "REBE_GROUP_JID": "120363000000000000@g.us",
    "WEBHOOK_SECRET": "webhook-secret-test",
    "REBE_DATABASE_URL": "postgresql://rebe:pw@bien-evo-pg:5432/rebe",
    "TELEGRAM_BOT_TOKEN": "telegram-token-test",
    "TELEGRAM_CHAT_ID": "-1001234567890",
    "KUMA_PUSH_URL": "http://kuma:3001/api/push/abc123",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A complete, mutable environment with every required variable present."""
    for name in Settings.variable_names():
        monkeypatch.delenv(name, raising=False)
    return dict(COMPLETE_ENV)


def test_loads_every_required_variable(env: dict[str, str]) -> None:
    settings = load_settings(env)

    assert settings.deepseek_api_key.get_secret_value() == "sk-deepseek-test"
    assert settings.evolution_api_url == "http://bien-evo:8080"
    assert settings.evolution_api_key.get_secret_value() == "evo-key-test"
    assert settings.rebe_group_jid == "120363000000000000@g.us"
    assert settings.webhook_secret.get_secret_value() == "webhook-secret-test"
    assert settings.rebe_database_url.get_secret_value().endswith("/rebe")
    assert settings.telegram_bot_token.get_secret_value() == "telegram-token-test"
    assert settings.telegram_chat_id == "-1001234567890"
    assert settings.kuma_push_url.get_secret_value() == "http://kuma:3001/api/push/abc123"


def test_optional_variables_have_spec_defaults(env: dict[str, str]) -> None:
    settings = load_settings(env)

    assert settings.evolution_instance == "bien-rebe"
    assert settings.timezone == "America/Mexico_City"
    assert settings.log_level == "INFO"


def test_evolution_instance_is_overridable_for_manual_failover(env: dict[str, str]) -> None:
    env["EVOLUTION_INSTANCE"] = "bien-backup"

    assert load_settings(env).evolution_instance == "bien-backup"


@pytest.mark.parametrize("missing", sorted(REQUIRED_VARIABLES))
def test_a_missing_required_variable_is_named(env: dict[str, str], missing: str) -> None:
    del env[missing]

    with pytest.raises(MissingConfigurationError) as caught:
        load_settings(env)

    assert caught.value.missing == [missing]
    assert missing in str(caught.value)


def test_every_missing_variable_is_named_at_once(env: dict[str, str]) -> None:
    del env["DEEPSEEK_API_KEY"]
    del env["KUMA_PUSH_URL"]

    with pytest.raises(MissingConfigurationError) as caught:
        load_settings(env)

    assert caught.value.missing == ["DEEPSEEK_API_KEY", "KUMA_PUSH_URL"]


def test_a_blank_required_variable_counts_as_missing(env: dict[str, str]) -> None:
    env["WEBHOOK_SECRET"] = "   "

    with pytest.raises(MissingConfigurationError) as caught:
        load_settings(env)

    assert caught.value.missing == ["WEBHOOK_SECRET"]


def test_the_deployment_spec_secrets_table_is_covered() -> None:
    """Every var in the deployment spec's section 4 table has a settings field."""
    spec_table = {
        "DEEPSEEK_API_KEY",
        "EVOLUTION_API_URL",
        "EVOLUTION_API_KEY",
        "EVOLUTION_INSTANCE",
        "WEBHOOK_SECRET",
        "REBE_DATABASE_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "KUMA_PUSH_URL",
        "TZ",
    }

    assert spec_table <= set(Settings.variable_names())


def test_env_example_documents_exactly_what_the_process_reads() -> None:
    """`.env.example` is the operator's copy of `Settings`; drift breaks deploys."""
    example = Path(__file__).resolve().parents[1] / ".env.example"
    documented = {
        line.split("=", 1)[0].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    assert documented == set(Settings.variable_names())


def test_an_invalid_variable_is_reported_with_its_reason(env: dict[str, str]) -> None:
    env["TZ"] = "Mars/Olympus_Mons"

    with pytest.raises(InvalidConfigurationError) as caught:
        load_settings(env)

    assert "TZ" in caught.value.problems
    assert "IANA" in caught.value.problems["TZ"]


def test_an_unparseable_url_is_rejected(env: dict[str, str]) -> None:
    env["EVOLUTION_API_URL"] = "bien-evo:8080"

    with pytest.raises(ValueError, match="EVOLUTION_API_URL"):
        load_settings(env)


def test_a_trailing_slash_on_the_evolution_url_is_normalised(env: dict[str, str]) -> None:
    env["EVOLUTION_API_URL"] = "http://bien-evo:8080/"

    assert load_settings(env).evolution_api_url == "http://bien-evo:8080"


def test_a_non_postgres_database_url_is_rejected(env: dict[str, str]) -> None:
    env["REBE_DATABASE_URL"] = "mysql://rebe:pw@host/rebe"

    with pytest.raises(ValueError, match="REBE_DATABASE_URL"):
        load_settings(env)


def test_an_unknown_timezone_is_rejected(env: dict[str, str]) -> None:
    env["TZ"] = "Mars/Olympus_Mons"

    with pytest.raises(ValueError, match="TZ"):
        load_settings(env)


def test_secrets_are_not_leaked_by_repr(env: dict[str, str]) -> None:
    rendered = repr(load_settings(env))

    for secret in ("sk-deepseek-test", "evo-key-test", "webhook-secret-test", "pw@bien-evo-pg"):
        assert secret not in rendered


def test_the_environment_is_read_only_through_load_settings(env: dict[str, str]) -> None:
    """`Settings()` reads the mapping it is handed, never the ambient process env."""
    with pytest.raises(MissingConfigurationError):
        load_settings({})
