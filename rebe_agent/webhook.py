"""The one endpoint Evolution posts to, and the token that guards it.

Section 2.2 of `docs/wayfinder/deployment-architecture-spec.md` puts the agent on
the internal Docker network with **no public FQDN**, and puts a secret in the
webhook path anyway. Both halves matter: the network is what stops the internet
reaching this, and the token is what stops anything else already inside the
network - Evolution itself, its Postgres, its Redis, another Coolify app - from
making Rebe talk. It is defence in depth, not the only defence.

Three decisions this module makes and keeps making:

**A wrong token is a 404, not a 403.** A 403 confirms the path exists and invites
guessing; a 404 tells a scanner exactly what an unused path tells it. The
comparison is `secrets.compare_digest`, so the answer does not arrive faster for
a token that shares a prefix with the real one.

**Every delivery that got past the token answers 200, whatever happened next.**
Evolution retries a webhook it believes failed, so a 500 on an unparseable body
would earn that same body again, forever; and a reply Rebe chose not to send is
not a failed delivery. The body is the same either way, so nothing can be probed
by watching the response.

**The work happens after the response.** A reply spends seconds looking like it
is being typed, and Evolution should not be holding a connection open through
them. The handler hands the event to a background task and answers immediately.

Two events arrive here, because the deployment spec has each instance subscribe
to two. `messages.upsert` is the reply leg's. `connection.update` is the link's,
and it goes to a `LinkWatch` - which is how a disconnect stops sending and a
reconnect puts Rebe back on the post-pairing ramp. This module decides neither;
it only makes sure the event gets to something that does.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from rebe_agent.inbound import ConnectionUpdate, InboundMessage, parse, parse_connection
from rebe_agent.reply import ReplyLeg
from rebe_agent.signals import LinkWatch

logger = logging.getLogger("rebe_agent.webhook")

WEBHOOK_PATH = "/webhook/{token}"
"""What Evolution's per-instance webhook is pointed at, secret and all."""

WEBHOOK_PORT = 8000
"""The port the container listens on internally. Never published to a host."""

WEBHOOK_HOST = "0.0.0.0"
"""Every interface *inside* the container, which is one internal network."""

ACCEPTED: dict[str, str] = {"status": "accepted"}
"""The only body this endpoint ever returns. It says nothing about Rebe."""


def build_app(leg: ReplyLeg, secret: str, *, link: LinkWatch | None = None) -> FastAPI:
    """The ASGI app: one route, one token, and the legs behind it.

    `link` is optional so that a test about the reply path does not have to stand
    up a ramp to exercise it. The serving process always wires one: without it,
    the disconnect that stops sending would never reach anything.
    """
    app = FastAPI(title="rebe-agent webhook", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post(WEBHOOK_PATH)
    async def receive(token: str, request: Request, background: BackgroundTasks) -> dict[str, str]:
        if not secrets.compare_digest(token, secret):
            logger.warning("a request arrived at the webhook path with the wrong token")
            raise HTTPException(status_code=404, detail="Not Found")

        body = await _body(request)
        if body is None:
            return dict(ACCEPTED)

        message = parse(body)
        if message is not None:
            background.add_task(_handle, leg, message)
            return dict(ACCEPTED)

        if link is not None:
            update = parse_connection(body)
            if update is not None:
                background.add_task(_link_changed, link, update)
                return dict(ACCEPTED)

        logger.debug("nothing to act on in this delivery")
        return dict(ACCEPTED)

    return app


async def _body(request: Request) -> dict[str, Any] | None:
    """The JSON object Evolution sent, or `None` for anything that is not one."""
    try:
        decoded = await request.json()
    except (ValueError, UnicodeDecodeError):
        logger.info("a delivery arrived that was not JSON")
        return None
    return decoded if isinstance(decoded, dict) else None


async def _handle(leg: ReplyLeg, message: InboundMessage) -> None:
    """Run the leg, and let nothing out of it reach the response or the group.

    `ReplyLeg.handle` already turns every expected failure into silence. This is
    the backstop for the unexpected one: a background task that raises would log
    a traceback and, more to the point, is the only place left where a bug could
    become something the group notices.
    """
    try:
        await leg.handle(message)
    except Exception:
        logger.exception("the reply leg failed on %s", message.message_id)


async def _link_changed(link: LinkWatch, update: ConnectionUpdate) -> None:
    """Report one connection state change, and let nothing out of it reach the wire.

    The watchtower already swallows its own failures. This is the backstop for
    the unexpected one, for the same reason `_handle` has one: a background task
    that raised would take the traceback and nothing else.
    """
    try:
        await link.connection_changed(update.state, reason=update.reason)
    except Exception:
        logger.exception("the link watch failed on a %r connection update", update.state)
