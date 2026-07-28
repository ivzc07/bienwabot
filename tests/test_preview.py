"""The preview lookup: a page's own `og:image`, or nothing.

Every page is served by an in-memory stub at the HTTP layer, so the assertions
are about real bytes over a real `httpx` client - the same seam the Evolution
and DeepSeek tests use. Nothing here touches the network.
"""

from __future__ import annotations

import httpx
import pytest

from rebe_agent.preview import preview_image_url

PAGE_URL = "https://openai.com/index/local-model"
IMAGE_URL = "https://openai.com/og/local-model.png"


def serving(*responses: httpx.Response) -> httpx.AsyncClient:
    """A client whose next request is answered with the first of `responses`."""
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: responses[0]))


async def test_a_page_with_an_og_image_answers_with_its_url() -> None:
    page = httpx.Response(
        200,
        html=f'<html><head><meta property="og:image" content="{IMAGE_URL}"></head></html>',
    )

    assert await preview_image_url(serving(page), PAGE_URL) == IMAGE_URL


async def test_a_page_with_only_a_twitter_image_falls_back_to_it() -> None:
    """Plenty of the vendor blogs declare the Twitter card and no Open Graph."""
    page = httpx.Response(
        200,
        html=f'<html><head><meta name="twitter:image" content="{IMAGE_URL}"></head></html>',
    )

    assert await preview_image_url(serving(page), PAGE_URL) == IMAGE_URL


async def test_a_page_with_no_preview_tag_is_no_image() -> None:
    page = httpx.Response(200, html="<html><head><title>Local model</title></head></html>")

    assert await preview_image_url(serving(page), PAGE_URL) is None


async def test_a_body_that_is_not_html_is_no_image() -> None:
    """An article URL that answers a feed or a plain-text error page is not a
    page, however much its bytes happen to look like one - so the declared
    content type decides before a single tag is read."""
    body = httpx.Response(
        200,
        content=f'<meta property="og:image" content="{IMAGE_URL}">'.encode(),
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )

    assert await preview_image_url(serving(body), PAGE_URL) is None


async def test_a_page_that_times_out_is_no_image() -> None:
    """The post is already written and waiting; a slow page is not worth holding
    it for, so the timeout reads exactly like a page with no image."""

    def hangs(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("the page never answered", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(hangs))

    assert await preview_image_url(client, PAGE_URL) is None


async def test_a_page_that_answers_an_error_is_no_image() -> None:
    """A 404's error page may well carry an og:image of its own; it is not the
    article's, so the status is read before a single tag."""
    error_page = httpx.Response(
        404,
        html=f'<html><head><meta property="og:image" content="{IMAGE_URL}"></head></html>',
    )

    assert await preview_image_url(serving(error_page), PAGE_URL) is None


@pytest.mark.parametrize(
    "junk",
    [
        "javascript:alert(document.domain)",
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ",
    ],
)
async def test_a_value_that_is_not_an_http_url_is_refused(junk: str) -> None:
    """The URL is handed to Evolution for WhatsApp's servers to fetch. A scheme
    they cannot fetch is not a picture, and a `javascript:` value is worse."""
    page = httpx.Response(
        200,
        html=f'<html><head><meta property="og:image" content="{junk}"></head></html>',
    )

    assert await preview_image_url(serving(page), PAGE_URL) is None


async def test_a_relative_image_is_resolved_against_the_pages_own_url() -> None:
    page = httpx.Response(
        200,
        html='<html><head><meta property="og:image" content="/og/local-model.png"></head></html>',
    )

    assert await preview_image_url(serving(page), PAGE_URL) == IMAGE_URL
