"""Runtime access requires CSRF, matching membership, origin, and issued tickets."""

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
from channels.db import database_sync_to_async
from channels.layers import channel_layers, get_channel_layer
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.core import signing
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from ..management.commands.runssh import SiteSSHServer, run_session
from ..models import Database, DatabaseHost, Site
from ..runtime_access import consume_access_token, issue_access_token
from ..runtime_consumers import SiteTerminalConsumer
from ..runtime_views import terminal_options


@pytest.fixture(autouse=True)
def runtime_memory(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    channel_layers.backends.clear()
    cache.clear()


@pytest.fixture
def runtime_site(student):
    site = Site.objects.create(name="runtime-site", mode="dynamic", purpose="project")
    site.users.add(student)
    return site


def test_tickets_are_one_time_bound_to_user_site_and_issuance(student, teacher, runtime_site):
    token = issue_access_token(student, runtime_site, "terminal")
    with pytest.raises(signing.BadSignature):
        consume_access_token(token, "terminal", teacher.pk, runtime_site.pk)
    assert consume_access_token(token, "terminal", student.pk, runtime_site.pk)["kind"] == "shell"
    with pytest.raises(signing.BadSignature):
        consume_access_token(token, "terminal", student.pk, runtime_site.pk)
    forged = signing.dumps(
        {"user": student.pk, "site": runtime_site.pk, "kind": "shell", "nonce": "unissued"},
        salt="director.runtime.terminal",
    )
    with pytest.raises(signing.BadSignature):
        consume_access_token(forged, "terminal", student.pk, runtime_site.pk)


def test_session_issue_requires_csrf_and_site_permission(student, teacher, runtime_site):
    client = Client(enforce_csrf_checks=True)
    client.force_login(student)
    url = reverse("sites:terminal_session", args=[runtime_site.pk])
    assert client.post(url, {"kind": "shell"}).status_code == 403
    client.get(reverse("sites:terminal", args=[runtime_site.pk]))
    csrf = client.cookies["csrftoken"].value
    response = client.post(url, {"kind": "shell", "csrfmiddlewaretoken": csrf})
    assert response.status_code == 200
    assert "token" in response.json()
    assert "no-store" in response["Cache-Control"]
    client.force_login(teacher)
    assert client.get(reverse("sites:terminal", args=[runtime_site.pk])).status_code == 404


def test_logs_are_escaped_and_internal_failures_not_exposed(client, student, runtime_site):
    client.force_login(student)
    with patch(
        "director.apps.sites.runtime_views.runtime_request",
        return_value={"output": "<script>private</script>", "truncated": False},
    ):
        response = client.get(reverse("sites:logs_output", args=[runtime_site.pk]))
    assert "&lt;script&gt;private&lt;/script&gt;" in response.content.decode()
    assert "<script>private</script>" not in response.content.decode()


def test_workspace_and_database_terminals_receive_only_site_credentials(runtime_site):
    host = DatabaseHost.objects.create(
        dbms="postgres",
        hostname="public-db.example",
        port=5432,
        admin_hostname="private-db.example",
        admin_port=5433,
        admin_username="administrator",
        admin_password="administrator-secret",
    )
    database = Database.objects.create(host=host, password="site:/@secret", provisioned=True)
    runtime_site.database = database
    runtime_site.save()
    workspace = terminal_options(runtime_site, "shell")
    database_cli = terminal_options(runtime_site, "database")
    assert workspace["environment"]["DIRECTOR_DATABASE_PASSWORD"] == database.password
    assert "site%3A%2F%40secret" in workspace["environment"]["DATABASE_URL"]
    assert database_cli["environment"] == {"PGPASSWORD": database.password}
    assert "public-db.example" in database_cli["command"]
    assert "administrator" not in str(workspace) + str(database_cli)
    assert "private-db.example" not in str(workspace) + str(database_cli)


def socket_for(site, user, token, origin="http://testserver"):
    encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
    socket = WebsocketCommunicator(
        SiteTerminalConsumer.as_asgi(),
        f"/sites/{site.pk}/terminal/",
        headers=[(b"origin", origin.encode()), (b"host", b"testserver")],
        subprotocols=["director-terminal", "ticket." + encoded],
    )
    socket.scope.update(user=user, url_route={"kwargs": {"site_id": site.pk}})
    return socket


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("bad_access", ("origin", "anonymous", "nonmember", "unaccepted"))
def test_terminal_rejects_bad_access(student, teacher, runtime_site, bad_access):
    token = issue_access_token(student, runtime_site, "terminal")
    user = (
        AnonymousUser()
        if bad_access == "anonymous"
        else teacher
        if bad_access == "nonmember"
        else student
    )
    if bad_access == "unaccepted":
        student.accepted_guidelines = False
        student.save()

    async def check():
        socket = socket_for(
            runtime_site,
            user,
            token,
            "https://elsewhere.example" if bad_access == "origin" else "http://testserver",
        )
        assert (await socket.connect())[0] is False
        await socket.disconnect()

    asyncio.run(check())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "revocation", ("membership", "inactive", "admin-role", "guidelines", "disabled-site")
)
def test_terminal_bytes_scoped_resize_and_current_access_revocation(
    student, runtime_site, revocation
):
    if revocation == "admin-role":
        runtime_site.users.clear()
        student.is_superuser = True
        student.save()
    token = issue_access_token(student, runtime_site, "terminal")

    def revoke():
        if revocation == "membership":
            runtime_site.users.clear()
        elif revocation == "disabled-site":
            runtime_site.availability = "disabled"
            runtime_site.save()
        else:
            field = {
                "inactive": "is_active",
                "admin-role": "is_superuser",
                "guidelines": "accepted_guidelines",
            }[revocation]
            setattr(student, field, False)
            student.save()

    async def check():
        class Upstream:
            def __init__(self):
                self.messages = asyncio.Queue()
                self.send = AsyncMock()
                self.close = AsyncMock(side_effect=lambda: self.messages.put_nowait(None))

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.messages.get()
                if item is None:
                    raise StopAsyncIteration
                return item

        upstream = Upstream()
        with patch(
            "director.apps.sites.runtime_consumers.open_runtime_terminal",
            AsyncMock(return_value=upstream),
        ):
            socket = socket_for(runtime_site, student, token)
            assert (await socket.connect())[0] is True
            assert await socket.receive_json_from() == {"type": "ready"}
            await upstream.messages.put(b"own site output")
            assert await socket.receive_from() == b"own site output"
            await socket.send_to(bytes_data=b"pwd\r")
            await socket.send_json_to({"type": "resize", "rows": 24, "cols": 80})
            await asyncio.sleep(0.05)
            assert upstream.send.await_args_list[0].args == (b"pwd\r",)
            await database_sync_to_async(revoke)()
            await get_channel_layer().group_send(
                runtime_site.channels_group_name(), {"type": "site.updated"}
            )
            message = await socket.receive_output()
            assert message["type"] == "websocket.close"
            assert message["code"] == 4403
            await socket.disconnect()
            upstream.close.assert_awaited()

    asyncio.run(check())


@pytest.mark.django_db(transaction=True)
def test_ssh_password_is_site_bound_one_time_and_no_forwarding(student, runtime_site):
    token = issue_access_token(student, runtime_site, "ssh")

    async def check():
        server = SiteSSHServer()
        server.connection_made(MagicMock())
        assert await server.validate_password("site-999", token) is False
        assert await server.validate_password(f"site-{runtime_site.pk}", token) is True
        assert await server.validate_password(f"site-{runtime_site.pk}", token) is False
        assert server.connection_requested("host", 22, "origin", 1234) is False
        assert server.server_requested("host", 22) is False

    asyncio.run(check())


@pytest.mark.django_db(transaction=True)
def test_actual_ssh_transport_accepts_site_ticket_and_rejects_host_exec(student, runtime_site):
    token = issue_access_token(student, runtime_site, "ssh")

    async def check():
        class Upstream:
            def __init__(self):
                self.messages = asyncio.Queue()

            async def send(self, data):
                if isinstance(data, bytes):
                    await self.messages.put(b"site-only-output\r\n")

            async def close(self):
                await self.messages.put(None)

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.messages.get()
                if item is None:
                    raise StopAsyncIteration
                return item

        key = asyncssh.generate_private_key("ssh-ed25519")
        listener = await asyncssh.create_server(
            SiteSSHServer,
            "127.0.0.1",
            0,
            server_host_keys=[key],
            process_factory=run_session,
            encoding=None,
            line_editor=False,
        )
        try:
            with patch(
                "director.apps.sites.management.commands.runssh.open_runtime_terminal",
                AsyncMock(return_value=Upstream()),
            ):
                async with asyncssh.connect(
                    "127.0.0.1",
                    listener.get_port(),
                    username=f"site-{runtime_site.pk}",
                    password=token,
                    known_hosts=([key.convert_to_public()], [], []),
                    encoding=None,
                ) as connection:
                    result = await connection.run("echo host-exec", check=False)
                    assert result.exit_status == 1
                    assert b"Only an interactive site terminal" in result.stderr
                    with pytest.raises(asyncssh.ChannelOpenError):
                        await connection.open_connection("localhost", 22)
                    process = await connection.create_process(term_type="xterm", term_size=(80, 24))
                    process.stdin.write(b"echo site-only\r")
                    output = await asyncio.wait_for(process.stdout.read(18), timeout=3)
                    assert output.startswith(b"site-only-output")
                    process.close()
                    await process.wait_closed()
        finally:
            listener.close()
            await listener.wait_closed()

    asyncio.run(check())
