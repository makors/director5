"""Proxy browser terminals after origin, session, ticket, and site checks."""

import asyncio
import base64
import binascii
import contextlib
import json
from urllib.parse import urlsplit

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.urls import path
from websockets.exceptions import WebSocketException

from .models import Site
from .runtime_access import consume_access_token
from .runtime_client import RuntimeUnavailable, open_runtime_terminal
from .runtime_views import terminal_options


def same_origin(scope):
    headers = dict(scope.get("headers", []))
    try:
        origin = urlsplit(headers.get(b"origin", b"").decode("ascii"))
        host = headers.get(b"host", b"").decode("ascii")
    except (ValueError, UnicodeError):
        return False
    return (
        origin.scheme in {"http", "https"}
        and bool(host)
        and origin.netloc.casefold() == host.casefold()
        and not origin.username
        and not origin.password
        and not origin.query
        and not origin.fragment
        and origin.path in {"", "/"}
        and (scope.get("scheme") != "wss" or origin.scheme == "https")
    )


def ticket_from_protocols(protocols):
    tickets = [protocol[7:] for protocol in protocols if protocol.startswith("ticket.")]
    if "director-terminal" not in protocols or len(tickets) != 1 or len(tickets[0]) > 6000:
        raise signing.BadSignature("Missing terminal ticket")
    try:
        return base64.b64decode(
            tickets[0] + "=" * (-len(tickets[0]) % 4), altchars=b"-_", validate=True
        ).decode("ascii")
    except (binascii.Error, UnicodeError) as error:
        raise signing.BadSignature("Invalid terminal ticket") from error


def authorized_runtime(user_id, site_id, kind="shell"):
    user = get_user_model().objects.get(pk=user_id, is_active=True)
    if getattr(settings, "DIRECTOR_REQUIRE_GUIDELINES", False) and not user.accepted_guidelines:
        raise Site.DoesNotExist
    site = (
        Site.objects.filter_editable(user)
        .select_related("database", "database__host")
        .get(pk=site_id)
    )
    return site, terminal_options(site, kind)


class SiteTerminalConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.upstream = None
        self.tasks = []
        self.group_name = None
        user = self.scope.get("user")
        if not user or not user.is_authenticated or not same_origin(self.scope):
            await self.close(code=4403)
            return
        try:
            self.site_id = int(self.scope["url_route"]["kwargs"]["site_id"])
            token = ticket_from_protocols(self.scope.get("subprotocols", []))
            self.user_id = user.pk
            data = await database_sync_to_async(consume_access_token)(
                token, "terminal", self.user_id, self.site_id
            )
            self.kind = data["kind"]
            site, options = await database_sync_to_async(authorized_runtime)(
                self.user_id, self.site_id, self.kind
            )
        except (
            ValueError,
            KeyError,
            signing.BadSignature,
            Site.DoesNotExist,
            get_user_model().DoesNotExist,
            RuntimeUnavailable,
        ):
            await self.close(code=4403)
            return
        self.group_name = site.channels_group_name()
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept(subprotocol="director-terminal")
        try:
            self.upstream = await open_runtime_terminal(self.site_id, options)
            await database_sync_to_async(authorized_runtime)(self.user_id, self.site_id, self.kind)
        except (RuntimeUnavailable, Site.DoesNotExist, get_user_model().DoesNotExist):
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "error",
                        "message": "This site's terminal is unavailable. Check that its workspace or database client image is available on the appserver.",
                    }
                )
            )
            await self.close_runtime()
            return
        await self.send(text_data=json.dumps({"type": "ready"}))
        self.tasks = [
            asyncio.create_task(self.forward_output()),
            asyncio.create_task(self.watch_access()),
        ]

    async def forward_output(self):
        try:
            async for message in self.upstream:
                if isinstance(message, bytes):
                    await self.send(bytes_data=message)
                else:
                    await self.send(text_data=message)
        except (WebSocketException, OSError):
            pass
        finally:
            await self.close_runtime()

    async def watch_access(self):
        deadline = asyncio.get_running_loop().time() + 3600
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(5)
            if not await self.access_is_current():
                await self.close_runtime(code=4403)
                return
        await self.close_runtime()

    async def access_is_current(self):
        try:
            await database_sync_to_async(authorized_runtime)(self.user_id, self.site_id, self.kind)
        except (Site.DoesNotExist, get_user_model().DoesNotExist, RuntimeUnavailable):
            return False
        return True

    async def site_updated(self, event):
        if not await self.access_is_current():
            await self.close_runtime(code=4403)

    async def site_deleted(self, event):
        await self.close_runtime(code=4404)

    async def operation_updated(self, event):
        if not await self.access_is_current():
            await self.close_runtime(code=4403)

    async def receive(self, text_data=None, bytes_data=None):
        if self.upstream is None:
            return
        if not await self.access_is_current():
            await self.close_runtime(code=4403)
            return
        try:
            if bytes_data is not None:
                if len(bytes_data) > 8192:
                    raise ValueError("Input is too large")
                await self.upstream.send(bytes_data)
            elif text_data is not None:
                if len(text_data) > 1024:
                    raise ValueError("Control message is too large")
                data = json.loads(text_data)
                if not isinstance(data, dict) or data.get("type") != "resize":
                    raise ValueError("Unsupported terminal control")
                rows, cols = data.get("rows"), data.get("cols")
                if (
                    type(rows) is not int
                    or type(cols) is not int
                    or not 2 <= rows <= 120
                    or not 10 <= cols <= 300
                ):
                    raise ValueError("Invalid terminal size")
                await self.upstream.send(json.dumps({"type": "resize", "rows": rows, "cols": cols}))
        except (ValueError, WebSocketException, OSError):
            await self.close_runtime(code=4400)

    async def close_runtime(self, code=1000):
        if self.upstream is not None:
            upstream, self.upstream = self.upstream, None
            with contextlib.suppress(WebSocketException, OSError):
                await upstream.close()
        await self.close(code=code)

    async def disconnect(self, close_code):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.upstream is not None:
            await self.upstream.close()
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)


urlpatterns = [path("sites/<int:site_id>/terminal/", SiteTerminalConsumer.as_asgi())]
