"""The article's own preview image, or nothing.

A WhatsApp link preview card renders in whatever language the article's Open
Graph tags were written, which for an AI launch is English. The post that
carries a *picture of the story* instead is the article's own `og:image`, sent
as a photo with her words as the caption. This module answers one question:
given the article's URL, what is its preview image's URL?

The answer is `None` far more often than it is a URL, and that is the design.
A page that times out, answers an error, serves no usable tag, or names a junk
URL all read the same way: no image. Nothing here raises, because the lookup
runs between "the post is written" and "the post goes out", and a missing
picture must never cost the post - the text message it replaces is exactly
today's behaviour, already paid for.

Two bounds keep the lookup cheap. The fetch is *streamed and capped*: an
`og:image` tag lives in the `<head>`, so reading stops at `</head>` or at a
byte ceiling, and a page that puts its head behind megabytes of inline script
is a page this run does without. And the tag is read with the stdlib HTML
parser rather than a regex, because `<meta content="..." property="og:image">`
is the same tag with the attributes the other way round, and attribute order
is not something a pattern can be trusted with.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser

import httpx

from rebe_agent.feeds import USER_AGENT

logger = logging.getLogger("rebe_agent.preview")

REQUEST_TIMEOUT_SECONDS = 10.0
"""Shorter than the feeds get. The post is already written and waiting on this
answer, and a slow page is answered the same way as a page with no image."""

MAX_HEAD_BYTES = 256 * 1024
"""Where reading stops. The preview tags live in the `<head>`; a page whose
head runs past a quarter of a megabyte is a page whose preview we do without,
and the cap is what keeps that page out of memory."""

PreviewLookup = Callable[[str], Awaitable[str | None]]
"""What the news leg is handed: a URL in, an image URL or `None` out."""


async def preview_image_url(http: httpx.AsyncClient, url: str) -> str | None:
    """The preview image `url`'s page declares, or `None`. Never raises.

    `http` is injected rather than owned, the way the feeds and the brain take
    theirs: the caller's client already carries the process's connection
    limits, and a test drives this function through a stub at the same seam.
    """
    try:
        async with http.stream(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as response:
            head = await _read_head(response)
    except httpx.HTTPError as exc:
        logger.info("no preview image from %s: %s", url, exc)
        return None
    return _declared_image(head)


async def _read_head(response: httpx.Response) -> bytes:
    """The start of the body, up to `</head>` or the byte cap, whichever is first."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes(65536):
        chunks.append(chunk)
        size += len(chunk)
        if b"</head>" in chunk or size >= MAX_HEAD_BYTES:
            break
    return b"".join(chunks)


class _MetaImages(HTMLParser):
    """The `og:image` and `twitter:image` values a page declares, first one each.

    Open Graph uses `property`, Twitter uses `name`, and either tag may be
    written self-closing - the parser's default `handle_startendtag` forwards
    those here. First wins, because a page that declares the tag twice is a
    page whose intent is not ours to pick through.
    """

    def __init__(self) -> None:
        super().__init__()
        self.og_image = ""
        self.twitter_image = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attributes = dict(attrs)
        content = (attributes.get("content") or "").strip()
        if not content:
            return
        key = attributes.get("property") or attributes.get("name") or ""
        if key == "og:image" and not self.og_image:
            self.og_image = content
        elif key == "twitter:image" and not self.twitter_image:
            self.twitter_image = content


def _declared_image(head: bytes) -> str | None:
    """The better of the two declarations, or `None` when the page has neither."""
    images = _MetaImages()
    images.feed(head.decode("utf-8", errors="replace"))
    return images.og_image or images.twitter_image or None
