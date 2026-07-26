"""Which items are big enough to break the plan, and which are not worth a slot.

Section 4 of `docs/wayfinder/posting-cadence-spec.md` splits every candidate in
two. **Normal tier** fills the rolled plan and nothing more. **High tier**
bypasses the daily target: it posts on top of the day, as soon as pacing allows.

An item is high tier when either of two things is true.

1. **Top of Hacker News by points, well above the ranker's normal floor.** Not
   "above the floor" - clearing the floor is what makes an item worth *ranking*.
   The bar is written as a multiple of that floor rather than as a second
   constant, so a posture that moves the floor moves this with it instead of
   quietly turning every ordinary item into breaking news.
2. **A first-party announcement of a model or product from a major AI org**, and
   not commentary about one. Two claims, answered separately: the source's own
   authority weight answers "major AI org, first party", and the headline
   answers "announcement".

**The headline decides whether something is an announcement, not the summary.**
A launch announces itself in its title - "Introducing X", "OpenAI ships Y",
"Presentamos Z" - while a summary is prose, and a stray "why" three sentences
into one is not an opinion piece. Reading the title alone is what keeps the
commentary veto from firing on the launch it was meant to let through.

The last thing here is the other half of the same rule: after the big item goes
out, a remaining slot whose best candidate is **weak** is pruned, because a
person who just shared the big thing does not also drop a mediocre link at 22:00.
Weak is the curator's own score against a bar, not a second opinion about
quality - there is one number that says how good an item is, and this reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from rebe_agent.curate import DEFAULT_FILTERS, DEFAULT_RANKING, Filters, Ranking, score
from rebe_agent.items import NewsItem, folded


class Tier(StrEnum):
    """How much of the day's shape an item is allowed to move.

    The value is what lands in the database, so these strings are stable.
    """

    NORMAL = "normal"
    """Fills the rolled plan and nothing more."""

    HIGH = "high"
    """Posts on top of the plan, bounded only by the anti-ban envelope."""


@dataclass(frozen=True, slots=True)
class TierBar:
    """Where the two bars sit, and what counts as a link not worth sharing."""

    points_multiple: float = 5.0
    """How far above the ranker's points floor "top of HN" starts.

    Five times the shipped floor is about 500 points, which on any given day is
    the handful of stories at the top of the front page rather than the couple of
    hundred that merely made it there.
    """

    first_party_authority: float = 0.85
    """The authority weight at which a source is announcing rather than reporting.

    A parameter and not a list of vendors, so a feed added to `rebe_agent.feeds`
    is classified on the weight it already carries and the tiers never have to
    know the source list exists. The shipped list puts OpenAI, DeepMind, Google
    AI and Hugging Face above this and the press below it.
    """

    weak_score: float = 0.5
    """Below this an item is filler rather than a link somebody wants.

    Calibrated against the shipped ranking: general tech press with no HN
    traction lands around 0.4, while a first-party launch or a well-upvoted HN
    story clears 0.6. So the slot that gets pruned after big news is the one
    whose best remaining candidate was going to be a roundup nobody upvoted.
    """


DEFAULT_BAR = TierBar()
"""The shipped posture, named so a caller need not rebuild it per candidate."""


LAUNCH_WORDS: tuple[str, ...] = (
    "introducing",
    "introduces",
    "introduce",
    "launch",
    "launches",
    "launching",
    "announcing",
    "announces",
    "announce",
    "releasing",
    "releases",
    "release",
    "ships",
    "shipping",
    "unveils",
    "unveiling",
    "now available",
    "available today",
    "rolling out",
    "lanza",
    "lanzan",
    "lanzamos",
    "lanzamiento",
    "presenta",
    "presentamos",
    "estrena",
    "ya disponible",
    "esta disponible",
)
"""What a headline says when something shipped, in both of the group's languages.

Every form is written out rather than stemmed: a stemmer would be a dependency
and a second thing to be wrong, and the set of ways an announcement announces
itself is small enough to read.
"""

COMMENTARY_WORDS: tuple[str, ...] = (
    "why",
    "what it means",
    "means for",
    "opinion",
    "analysis",
    "hands on",
    "review",
    "reaction",
    "explained",
    "explainer",
    "first impressions",
    "we tried",
    "our take",
    "takeaways",
    "everything you need to know",
    "how to",
    "thoughts on",
    "the case for",
    "the case against",
    "por que",
    "que significa",
    "lo que significa",
    "analisis",
    "resena",
    "probamos",
    "asi funciona",
)
"""What a headline says when it is *about* a launch. These veto the words above.

The veto is one-way on purpose. "Why OpenAI's launch matters" carries a launch
word and is still commentary; nothing that reads as commentary is ever an
announcement, so this list wins wherever both match.
"""


def classify(
    item: NewsItem,
    *,
    filters: Filters = DEFAULT_FILTERS,
    bar: TierBar = DEFAULT_BAR,
) -> Tier:
    """Which tier one candidate belongs to. Deterministic, and free.

    No model call and no clock: the tier is a property of the item and of the
    posture, so the same candidate classifies the same way at 03:00 and at noon -
    which is what lets the overnight queue hold something and be sure it is still
    the big story when the morning window opens.
    """
    if item.points is not None and item.points >= filters.points_floor * bar.points_multiple:
        return Tier.HIGH
    if item.authority >= bar.first_party_authority and announces_a_launch(item):
        return Tier.HIGH
    return Tier.NORMAL


def announces_a_launch(item: NewsItem) -> bool:
    """Does the headline say something shipped, rather than talk about something
    that did?"""
    headline = folded(item.title)
    if _says(headline, COMMENTARY_WORDS):
        return False
    return _says(headline, LAUNCH_WORDS)


def _says(text: str, phrases: tuple[str, ...]) -> bool:
    """Whether any phrase appears in `text` as whole words.

    `folded` has already reduced the headline to space-separated words, so
    padding both sides is the whole of word-boundary matching here - and it is
    what stops "release" from matching inside "released" only by accident, or
    "why" from matching inside "whywhat".
    """
    padded = f" {text} "
    return any(f" {phrase} " in padded for phrase in phrases)


def weak(
    item: NewsItem,
    now: datetime,
    *,
    ranking: Ranking = DEFAULT_RANKING,
    bar: TierBar = DEFAULT_BAR,
) -> bool:
    """Is this the mediocre link a person would not bother sharing at 22:00?"""
    return score(item, now, ranking) < bar.weak_score
