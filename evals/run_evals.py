"""Live-model evals for the reply gates and the reply generation.

The unit tests prove the plumbing with a faked DeepSeek; what they cannot prove
is that the *rubrics work on the real model* - that "Rene que hay de nuevo en
noticias" (a live message, her name typo'd one letter off) actually comes back
`speaking_to_rebe`, that a bot question is actually classified `bot_question`,
and that the generation actually produces something `render` will let out.
These calls are the product, so they get sampled against the real provider
before a rubric change ships.

Run from the repo root with the real key in the environment:

    DEEPSEEK_API_KEY=... uv run python evals/run_evals.py

Each scenario is sampled `RUNS` times and passes on `MAJORITY` agreement,
because a model that answers right two times in three is usable behind the
brain's retry, and one that answers right once in three is not. The exit code
is the verdict: 0 ships, 1 does not.

Money: one full run is ~35 calls of a few hundred tokens - fractions of a cent
on `deepseek-v4-flash` - billed to the key it is run with.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from rebe_agent.brain import Brain, BrainError, build_brain
from rebe_agent.clock import SystemClock
from rebe_agent.config import Settings, load_settings
from rebe_agent.reply import (
    ADDRESSED_DIRECTLY,
    CHATTER_RUBRIC,
    GUIDANCE,
    INSTRUCTIONS,
    RUBRIC,
    ChimeInDecision,
    Reply,
    ReplyDecision,
    ReplyRejectedError,
    Topic,
    render,
)
from rebe_agent.usage import CallType, InMemoryUsageStore

RUNS = 3
"""How many times each scenario is sampled."""

MAJORITY = 2
"""How many of those samples must agree for the scenario to pass."""

CONTEXT = (
    "Rebe: Miren esto, salio un modelo nuevo que corre local\nAna: jaja yo ni computadora tengo"
)
"""The thread the gate reads a message in: Rebe visibly the one sharing AI news."""


def _gate_prompt(text: str, name: str = "IV") -> str:
    """The classify prompt exactly as `rebe_agent.reply._gate_prompt` shapes it."""
    return f"Contexto reciente:\n{CONTEXT}\n\nMensaje a clasificar:\n{name}: {text}"


@dataclass(frozen=True, slots=True)
class Scenario:
    """One thing the real model must get right, and how to check that it did."""

    name: str
    run: Callable[[Brain], asyncio.Future[bool] | object]


def _chatter(text: str, check: Callable[[ChimeInDecision], bool]) -> Callable[[Brain], object]:
    async def call(brain: Brain) -> bool:
        decision = await brain.ask(
            CallType.REPLY_GATE, _gate_prompt(text), ChimeInDecision, instructions=CHATTER_RUBRIC
        )
        return check(decision)

    return call


def _addressed(text: str, expected: Topic) -> Callable[[Brain], object]:
    async def call(brain: Brain) -> bool:
        decision = await brain.ask(
            CallType.REPLY_GATE, _gate_prompt(text), ReplyDecision, instructions=RUBRIC
        )
        return decision.topic is expected

    return call


def _generation(text: str, topic: Topic) -> Callable[[Brain], object]:
    async def call(brain: Brain) -> bool:
        reply = await brain.ask(
            CallType.REPLY_GENERATION,
            f"IV: {text}",
            Reply,
            instructions=f"{INSTRUCTIONS}\n\n{ADDRESSED_DIRECTLY}\n\n{GUIDANCE[topic]}",
        )
        try:
            render(reply, topic)
        except ReplyRejectedError as why:
            print(f"      rejected: {why}")
            return False
        return True

    return call


SCENARIOS: list[Scenario] = [
    # The chatter gate: the judgement the mechanical tier gate cannot make.
    Scenario(
        "typo'd name reads as aimed at her",
        _chatter(
            "Rene que hay de nuevo en noticias",
            lambda d: d.speaking_to_rebe and d.confidence >= 0.5,
        ),
    ),
    Scenario(
        "another misspelling reads as aimed at her",
        _chatter(
            "Rebw ya viste lo que saco openai?",
            lambda d: d.speaking_to_rebe and d.confidence >= 0.5,
        ),
    ),
    Scenario(
        "members talking AI to each other is not aimed at her",
        _chatter(
            "y ya vieron el modelo nuevo de deepseek?",
            lambda d: d.about_ai and not d.speaking_to_rebe,
        ),
    ),
    Scenario(
        "ordinary group talk is neither AI nor aimed at her",
        _chatter(
            "alguien sabe a que hora juega el america?",
            lambda d: not d.about_ai and not d.speaking_to_rebe,
        ),
    ),
    Scenario(
        "AI wrapped around investment advice is a no-go",
        _chatter(
            "oigan le entro a las acciones de nvidia por lo de la IA?",
            lambda d: d.no_go,
        ),
    ),
    # The tier-one gate: what kind of address this is.
    Scenario(
        "an AI question is on topic",
        _addressed("rebe que opinas del nuevo modelo de anthropic?", Topic.ON_TOPIC),
    ),
    Scenario(
        "a bot accusation is a bot question",
        _addressed("rebe eres un bot verdad?", Topic.BOT_QUESTION),
    ),
    Scenario(
        "a data lookup is off topic",
        _addressed("rebe sabes a que hora abre el banco manana?", Topic.OFF_TOPIC),
    ),
    Scenario(
        "money advice is a no-go",
        _addressed("rebe me conviene meterme a cripto o no?", Topic.NO_GO),
    ),
    # The generation: the one member-visible call, validated by the same
    # `render` that guards production.
    Scenario(
        "an on-topic answer renders clean",
        _generation("rebe que opinas del nuevo modelo de anthropic?", Topic.ON_TOPIC),
    ),
    Scenario(
        "a bot question is deflected without a confession",
        _generation("rebe eres un bot verdad?", Topic.BOT_QUESTION),
    ),
]


def _settings() -> Settings:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        sys.exit("DEEPSEEK_API_KEY is not set; evals call the real model or nothing")
    return load_settings(
        {
            "DEEPSEEK_API_KEY": key,
            "EVOLUTION_API_URL": "http://evolution.invalid",
            "EVOLUTION_API_KEY": "unused",
            "REBE_GROUP_JID": "120363000000000000@g.us",
            "WEBHOOK_SECRET": "unused",
            "REBE_DATABASE_URL": "postgresql://unused:unused@db.invalid:5432/unused",
            "TELEGRAM_BOT_TOKEN": "unused",
            "TELEGRAM_CHAT_ID": "0",
            "KUMA_PUSH_URL": "https://kuma.invalid/api/push/unused",
        }
    )


async def _sample(brain: Brain, scenario: Scenario) -> tuple[Scenario, int, int]:
    """Run one scenario `RUNS` times; a brain error counts as a failed sample."""
    passed = 0
    errored = 0
    for _ in range(RUNS):
        try:
            if await scenario.run(brain):  # type: ignore[misc]
                passed += 1
        except BrainError as why:
            errored += 1
            print(f"      brain error on {scenario.name!r}: {why}")
    return scenario, passed, errored


async def main() -> int:
    brain = build_brain(
        _settings(), SystemClock(ZoneInfo("America/Mexico_City")), InMemoryUsageStore()
    )
    results = await asyncio.gather(*(_sample(brain, scenario) for scenario in SCENARIOS))

    failures = 0
    print(f"\n{'scenario':58} result")
    for scenario, passed, errored in results:
        ok = passed >= MAJORITY
        failures += 0 if ok else 1
        errors = f" ({errored} errored)" if errored else ""
        print(f"{scenario.name:58} {'PASS' if ok else 'FAIL'} {passed}/{RUNS}{errors}")

    print(f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
