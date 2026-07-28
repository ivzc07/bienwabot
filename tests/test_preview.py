"""The preview lookup: a page's own `og:image`, or nothing.

Every page is served by an in-memory stub at the HTTP layer, so the assertions
are about real bytes over a real `httpx` client - the same seam the Evolution
and DeepSeek tests use. Nothing here touches the network.
"""

from __future__ import annotations

import httpx

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
