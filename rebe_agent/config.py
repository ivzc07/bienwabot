"""The one place this process reads its environment.

Every secret and setting in section 4 of `docs/wayfinder/deployment-architecture-spec.md`
lands in `Settings`. This module is the only place the codebase *reads* the
environment: call `load_settings()` once at boot and pass the result down.

A missing or malformed variable raises `ConfigurationError` naming the variable,
so the container dies at boot with a readable reason rather than at first use.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")

DEFAULT_TIMEZONE = "America/Mexico_City"
DEFAULT_INSTANCE = "bien-rebe"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class ConfigurationError(ValueError):
    """The environment cannot produce a usable `Settings`."""


class MissingConfigurationError(ConfigurationError):
    """One or more required variables are absent or blank."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        listed = ", ".join(missing)
        super().__init__(
            f"Missing required environment variable(s): {listed}. "
            f"See .env.example for what each one is for."
        )


class InvalidConfigurationError(ConfigurationError):
    """One or more variables are present but unusable."""

    def __init__(self, problems: dict[str, str]) -> None:
        self.problems = problems
        listed = "; ".join(f"{name}: {reason}" for name, reason in problems.items())
        super().__init__(f"Invalid environment variable(s): {listed}")


def _absolute_url(value: str, *, allowed_schemes: tuple[str, ...] = ("http", "https")) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in allowed_schemes or not parts.netloc:
        allowed = " or ".join(f"{scheme}://" for scheme in allowed_schemes)
        raise ValueError(f"must be an absolute {allowed} URL, got {value!r}")
    return value.strip().rstrip("/")


class Settings(BaseModel):
    """Typed, frozen configuration for one `rebe-agent` process.

    Field names are Python-side; the aliases are the environment variable names
    from the deployment spec, and are the only names an operator ever sets.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=False)

    deepseek_api_key: SecretStr = Field(alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default=DEFAULT_DEEPSEEK_BASE_URL, alias="DEEPSEEK_BASE_URL")
    evolution_api_url: str = Field(alias="EVOLUTION_API_URL")
    evolution_api_key: SecretStr = Field(alias="EVOLUTION_API_KEY")
    evolution_instance: str = Field(default=DEFAULT_INSTANCE, alias="EVOLUTION_INSTANCE")
    rebe_group_jid: str = Field(alias="REBE_GROUP_JID")
    webhook_secret: SecretStr = Field(alias="WEBHOOK_SECRET")
    rebe_database_url: SecretStr = Field(alias="REBE_DATABASE_URL")
    telegram_bot_token: SecretStr = Field(alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(alias="TELEGRAM_CHAT_ID")
    kuma_push_url: SecretStr = Field(alias="KUMA_PUSH_URL")
    timezone: str = Field(default=DEFAULT_TIMEZONE, alias="TZ")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("evolution_api_url", "deepseek_base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _absolute_url(value)

    @field_validator("rebe_database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        _absolute_url(
            value.get_secret_value(),
            allowed_schemes=("postgresql", "postgres", "postgresql+psycopg"),
        )
        return value

    @field_validator("kuma_push_url")
    @classmethod
    def _check_push_url(cls, value: SecretStr) -> SecretStr:
        """A secret like the rest of them: the push token is *in* the URL, so
        anybody holding it can keep the monitor green while Rebe is dead."""
        _absolute_url(value.get_secret_value())
        return value

    @field_validator("evolution_instance", "rebe_group_jid", "telegram_chat_id")
    @classmethod
    def _check_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        name = value.strip()
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"must be a valid IANA timezone name, got {value!r}") from exc
        return name

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"must be one of {', '.join(LOG_LEVELS)}, got {value!r}")
        return level

    @property
    def zone(self) -> ZoneInfo:
        """The configured timezone as a `tzinfo`, ready to hand to a clock."""
        return ZoneInfo(self.timezone)

    @classmethod
    def variable_names(cls) -> tuple[str, ...]:
        """Every environment variable this process reads, in declaration order."""
        return tuple(field.alias or name for name, field in cls.model_fields.items())


REQUIRED_VARIABLES: tuple[str, ...] = tuple(
    field.alias or name for name, field in Settings.model_fields.items() if field.is_required()
)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build `Settings` from an environment mapping (defaults to `os.environ`).

    Blank and whitespace-only values are treated as absent, so a variable that
    Coolify injects empty fails the same way as one that was never set.
    """
    if env is None:
        env = os.environ

    supplied: dict[str, str] = {
        name: env[name].strip()
        for name in Settings.variable_names()
        if name in env and env[name].strip()
    }

    try:
        return Settings.model_validate(supplied)
    except ValidationError as exc:
        missing: list[str] = []
        problems: dict[str, str] = {}
        for error in exc.errors():
            name = str(error["loc"][0]) if error["loc"] else "<unknown>"
            if error["type"] == "missing":
                missing.append(name)
            else:
                problems[name] = error["msg"].removeprefix("Value error, ")
        if missing:
            raise MissingConfigurationError(sorted(missing)) from exc
        raise InvalidConfigurationError(problems) from exc
