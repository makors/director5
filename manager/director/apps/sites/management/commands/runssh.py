"""Serve site-container SSH sessions using one-time Manager-issued passwords."""

import asyncio
import contextlib
import json
import re

import asyncssh
from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.management.base import BaseCommand, CommandError
from redis.exceptions import RedisError
from websockets.exceptions import WebSocketException

from ...models import Site
from ...runtime_access import consume_access_token, load_access_token
from ...runtime_client import RuntimeUnavailable, open_runtime_terminal
from ...runtime_consumers import authorized_runtime


class SiteSSHServer(asyncssh.SSHServer):
    def connection_made(self, connection):
        self.connection = connection
        self.user_id = None
        self.site_id = None

    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    async def validate_password(self, username, password):
        match = re.fullmatch(r"site-([1-9][0-9]*)", username)
        if not match:
            return False
        try:
            payload = load_access_token(password, "ssh")
            site_id = int(match[1])
            if payload["site"] != site_id or payload["kind"] != "shell":
                return False
            await database_sync_to_async(authorized_runtime)(payload["user"], site_id)
            await database_sync_to_async(consume_access_token)(
                password, "ssh", payload["user"], site_id
            )
        except (
            signing.BadSignature,
            RuntimeUnavailable,
            Site.DoesNotExist,
            get_user_model().DoesNotExist,
            RedisError,
            OSError,
            ValueError,
        ):
            return False
        self.user_id, self.site_id = payload["user"], site_id
        self.connection.set_extra_info(runtime_user_id=self.user_id, runtime_site_id=self.site_id)
        return True

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return False

    def server_requested(self, listen_host, listen_port):
        return False


async def forward_ssh_input(process, upstream, user_id, site_id):
    while True:
        try:
            data = await process.stdin.read(8192)
        except asyncssh.TerminalSizeChanged:
            continue
        except asyncssh.SignalReceived as signal:
            data = b"\x03" if signal.signal == "INT" else b"\x04"
        if not data:
            return
        await database_sync_to_async(authorized_runtime)(user_id, site_id)
        await upstream.send(data)


async def forward_ssh_output(process, upstream):
    async for data in upstream:
        if isinstance(data, bytes):
            process.stdout.write(data)
            await process.stdout.drain()


async def watch_ssh_access(process, upstream, user_id, site_id):
    previous_size = None
    while True:
        await database_sync_to_async(authorized_runtime)(user_id, site_id)
        width, height = process.get_terminal_size()[:2]
        size = (max(2, min(120, height)), max(10, min(300, width)))
        if size != previous_size:
            await upstream.send(json.dumps({"type": "resize", "rows": size[0], "cols": size[1]}))
            previous_size = size
        await asyncio.sleep(2)


async def run_session(process):
    upstream = None
    tasks = []
    if process.command or getattr(process, "subsystem", None) or not process.term_type:
        process.stderr.write(b"Only an interactive site terminal is supported.\r\n")
        process.exit(1)
        return
    user_id = process.get_extra_info("runtime_user_id")
    site_id = process.get_extra_info("runtime_site_id")
    try:
        _, options = await database_sync_to_async(authorized_runtime)(user_id, site_id)
        upstream = await open_runtime_terminal(site_id, options)

        tasks = [
            asyncio.create_task(forward_ssh_input(process, upstream, user_id, site_id)),
            asyncio.create_task(forward_ssh_output(process, upstream)),
            asyncio.create_task(watch_ssh_access(process, upstream, user_id, site_id)),
        ]
        await asyncio.wait(tasks, timeout=3600, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            if task.done() and not task.cancelled():
                task.result()
    except (
        RuntimeUnavailable,
        WebSocketException,
        OSError,
        Site.DoesNotExist,
        get_user_model().DoesNotExist,
    ):
        process.stderr.write(b"The site's running terminal is unavailable.\r\n")
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if upstream is not None:
            with contextlib.suppress(WebSocketException, OSError):
                await upstream.close()
        process.exit(0)


class Command(BaseCommand):
    help = "Run the site-only SSH server. Requires a persistent SSH host key."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=settings.DIRECTOR_SSH_PORT)
        parser.add_argument("--host-key", default=settings.DIRECTOR_SSH_HOST_KEY)

    def handle(self, *args, **options):
        if not options["host_key"]:
            raise CommandError(
                "Configure DIRECTOR_SSH_HOST_KEY or pass --host-key with a persistent private host key file."
            )
        cache_backend = settings.CACHES["default"]["BACKEND"]
        if not settings.DEBUG and "locmem" in cache_backend:
            raise CommandError("SSH passwords require a shared cache in production.")

        async def serve():
            listener = await asyncssh.create_server(
                SiteSSHServer,
                options["host"],
                options["port"],
                server_host_keys=[options["host_key"]],
                process_factory=run_session,
                encoding=None,
                line_editor=False,
                allow_scp=False,
                agent_forwarding=False,
                x11_forwarding=False,
                public_key_auth=False,
                kbdint_auth=False,
            )
            async with listener:
                await listener.wait_closed()

        try:
            asyncio.run(serve())
        except (OSError, asyncssh.Error) as error:
            raise CommandError(
                "The SSH listener could not start. Check its bind address, port, and host key."
            ) from error
