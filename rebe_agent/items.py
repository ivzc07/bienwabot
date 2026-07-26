"""What a news candidate is, and the three ways two candidates can be the same.

Section 2 of `docs/wayfinder/news-pipeline-research.md` settles the shape of the
anti-repost gate, and the reason it is deterministic rather than semantic is
worth restating: DeepSeek exposes no embeddings endpoint, so there is nothing to
measure "nearly the same story" with. Three cheap layers are the whole gate, and
each one exists for a collision the others miss:

1. **The stable source ID.** HN hands back an integer `objectID`, an RSS item a
   `guid`. Catches the same item seen twice from the same place.
2. **The canonical URL.** Scheme and host normalised, tracking parameters
   stripped, fragment and trailing slash dropped. Catches the same article
   surfaced by HN *and* by the vendor's own feed - which is the common case, and
   the one a naive URL comparison always misses.
3. **The hash of the normalised title.** Catches the same story reposted under a
   different URL: a syndication, a moved page, a vendor blog picked up elsewhere.

A repost is the most visible bot tell there is, so these keys live in one module
that both the store and the ranker read, rather than being recomputed - slightly
differently - at each call site.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

WEB_SCHEMES = ("http", "https")
"""What counts as a resolvable link. Everything else is not an article."""

CANONICAL_SCHEME = "https"
"""Every canonical URL is written as https, so http and https are one key.

The two are the same article in every case that matters here, and a vendor feed
serving http while HN carries https is exactly the collision layer 2 is for.
"""

DEFAULT_PORTS = frozenset({80, 443})

TRACKING_PREFIXES = ("utm_", "at_", "_hs")
"""Whole families of campaign parameters, matched by prefix rather than listed."""

TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "twclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "cmpid",
        "spm",
        "ref",
        "ref_src",
        "ref_url",
    }
)
"""Named trackers, kept deliberately conservative.

Anything not on this list survives, because a stripped parameter that the page
actually needed turns one article into a broken link - a worse failure than the
duplicate it was trying to prevent. `?id=`, `?p=` and friends are the article.
"""


def shareable_url(raw: str) -> str:
    """The address the group gets: the source's own link, minus the tracking.

    Deliberately *not* the canonical form. Canonicalising forces https, drops a
    `www.` and reorders the query, all of which are right for comparing two links
    and wrong for following one: a host that serves only `www.`, or only http,
    would get a dead link posted to it. So the article is shared as its publisher
    wrote it, with the campaign parameters and the fragment taken off, and the
    rewriting is kept to the key nobody ever clicks.
    """
    parts = urlsplit(raw.strip())
    if parts.scheme.lower() not in WEB_SCHEMES or not parts.netloc:
        return ""
    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(name)
    ]
    # Left in the publisher's order, unlike the key: this one is read by people.
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(kept), ""))


def canonical_url(raw: str) -> str:
    """The comparable form of a link, or `""` if it is not a resolvable article.

    Answering with an empty string rather than raising is deliberate: "no usable
    URL" is one of the quality filters, so it is checked in the same pass as a
    missing title instead of through exception flow on every candidate.
    """
    parts = urlsplit(raw.strip())
    if parts.scheme.lower() not in WEB_SCHEMES:
        return ""

    try:
        host, port = parts.hostname, parts.port
    except ValueError:  # a port that is not a number; not an address we can use
        return ""
    if not host:
        return ""

    host = host.removeprefix("www.")
    netloc = host if port is None or port in DEFAULT_PORTS else f"{host}:{port}"

    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(name)
    ]
    # Sorted, because two links that differ only in parameter order are one page,
    # and a dedup key that says otherwise posts the article twice.
    query = urlencode(sorted(kept))

    path = parts.path.rstrip("/")
    return urlunsplit((CANONICAL_SCHEME, netloc, path, query, ""))


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def title_hash(title: str) -> str:
    """A stable hash of the headline, blind to case, accents and punctuation.

    Mexican Spanish headlines lose accents in the wild constantly - a feed
    summary drops them, an aggregator re-types them - and "Un modelo mas rapido"
    is not a second story. Folding them away is what makes this layer catch the
    repost it exists for rather than only exact reprints.
    """
    decomposed = unicodedata.normalize("NFKD", title)
    unaccented = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    words = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in unaccented).split()
    return hashlib.sha256(" ".join(words).casefold().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NewsItem:
    """One candidate, from whichever source found it.

    `authority` is the source's own weight rather than a lookup the ranker does,
    so a feed added to `rebe_agent.feeds` carries its standing with it and the
    ranker never has to know the source list exists.
    """

    source: str
    """Where it came from: `hackernews`, or a feed key such as `openai`."""

    source_id: str
    """That source's own stable ID: an HN `objectID`, an RSS `guid`."""

    title: str
    url: str
    published_at: datetime
    """Timezone-aware, always. Freshness is measured against it."""

    authority: float = 0.5
    """How much this source's word is worth, in `[0, 1]`. A first-party launch
    announcement outranks a roundup of one."""

    points: int | None = None
    """HN points. `None` means the source has no popularity signal at all, which
    is not the same as an item nobody upvoted."""

    comments: int | None = None
    summary: str = ""
    """Whatever short text the source supplied, already stripped of markup."""

    @property
    def source_key(self) -> tuple[str, str]:
        """Layer 1: the same item seen twice from the same place."""
        return (self.source, self.source_id)

    @property
    def canonical_url(self) -> str:
        """Layer 2: the same article surfaced by two different sources."""
        return canonical_url(self.url)

    @property
    def link(self) -> str:
        """What actually gets posted. Compared on `canonical_url`, followed on this."""
        return shareable_url(self.url)

    @property
    def title_hash(self) -> str:
        """Layer 3: the same story reposted under a different URL."""
        return title_hash(self.title)

    @property
    def grounding(self) -> str:
        """Everything the framing line is allowed to restate.

        Section "Anti-hallucination" of `docs/wayfinder/reply-policy-spec.md`
        bounds the post to what the source item said. This is that source text,
        in one place, so the prompt and the check that enforces it can never be
        looking at different things.
        """
        return f"{self.title}\n{self.summary}".strip()


class SeenItems:
    """Items already accounted for, and the one place "already" is decided.

    Two callers ask that question - the curator collapsing duplicates inside one
    run, and the in-memory posted store answering across runs - and a fourth
    layer added to one and not the other would be a repost nobody could explain.
    So the rule lives with the keys it reads rather than being spelled out at each
    call site. The Postgres store restates it in SQL because SQL is where its copy
    of the rule has to run; those three columns are the same three keys.
    """

    def __init__(self) -> None:
        self._sources: set[tuple[str, str]] = set()
        self._urls: set[str] = set()
        self._titles: set[str] = set()

    def knows(self, item: NewsItem) -> bool:
        """True if any one of the three layers has seen this item before."""
        return (
            item.source_key in self._sources
            or item.canonical_url in self._urls
            or item.title_hash in self._titles
        )

    def remember(self, item: NewsItem) -> None:
        self._sources.add(item.source_key)
        self._urls.add(item.canonical_url)
        self._titles.add(item.title_hash)
