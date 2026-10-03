"""Verify real live dashboard access, escaping, and working forms."""

import asyncio

import pytest
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser

from ..consumers import SiteInfoConsumer
from ..models import Action, Operation, Site


@pytest.fixture(autouse=True)
def memory_channels(settings):
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


def communicator_for(site_id, user, query=""):
    communicator = WebsocketCommunicator(SiteInfoConsumer.as_asgi(), f"/sites/{site_id}/" + query)
    communicator.scope.update(
        {
            "user": user,
            "url_route": {"kwargs": {"site_id": site_id}},
            "cookies": {"csrftoken": "a" * 32},
        }
    )
    return communicator


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("user_type", ("anonymous", "other", "missing"))
def test_inaccessible_socket_closes_cleanly(student, teacher, user_type):
    site = Site.objects.create(name="private-site", mode="static", purpose="project")
    site.users.add(student)
    user = AnonymousUser() if user_type == "anonymous" else teacher
    site_id = site.id if user_type != "missing" else site.id + 1

    async def check():
        communicator = communicator_for(site_id, user)
        connected, code = await communicator.connect()
        assert connected is False
        assert code in {4401, 4404}
        await communicator.disconnect()

    asyncio.run(check())


@pytest.mark.django_db(transaction=True)
def test_live_fragment_escapes_content_keeps_csrf_and_hides_admin_logs(student):
    site = Site.objects.create(
        name="test-site",
        mode="static",
        purpose="project",
        description="<script>description</script>",
    )
    site.users.add(student)
    operation = Operation.objects.create(site=site, ty="create_site")
    Action.objects.create(
        operation=operation,
        slug="build_image",
        name="Building image",
        result=False,
        message="private admin diagnostics",
        user_message="<script>user error</script>",
    )

    async def check():
        communicator = communicator_for(site.id, student)
        assert (await communicator.connect())[0] is True
        html = await communicator.receive_from()
        assert 'id="site-status"' in html
        assert "&lt;script&gt;description&lt;/script&gt;" in html
        assert "&lt;script&gt;user error&lt;/script&gt;" in html
        assert "private admin diagnostics" not in html
        assert 'name="csrfmiddlewaretoken" value="' + "a" * 32 + '"' in html
        assert "Retry operation" in html
        layer = get_channel_layer()
        assert site.channels_group_name() in layer.groups
        await communicator.disconnect()
        assert site.channels_group_name() not in layer.groups

    asyncio.run(check())


@pytest.mark.django_db(transaction=True)
def test_membership_revocation_closes_connected_dashboard(student):
    site = Site.objects.create(name="test-site", mode="static", purpose="project")
    site.users.add(student)

    async def check():
        communicator = communicator_for(site.id, student)
        assert (await communicator.connect())[0] is True
        await communicator.receive_from()
        await database_sync_to_async(site.users.clear)()
        await get_channel_layer().group_send(site.channels_group_name(), {"type": "site.updated"})
        html = await communicator.receive_from()
        assert "Site unavailable" in html
        message = await communicator.receive_output()
        assert message["type"] == "websocket.close"
        assert message["code"] == 4404
        await communicator.disconnect()

    asyncio.run(check())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("field", ("is_active", "accepted_guidelines", "is_superuser"))
def test_stale_session_permissions_close_dashboard(student, field):
    site = Site.objects.create(name="protected-site", mode="static", purpose="project")
    if field == "is_superuser":
        student.is_superuser = True
        student.save(update_fields=["is_superuser"])
    else:
        site.users.add(student)

    async def check():
        communicator = communicator_for(site.id, student)
        assert (await communicator.connect())[0] is True
        await communicator.receive_from()
        await database_sync_to_async(type(student).objects.filter(pk=student.pk).update)(
            **{field: False}
        )
        await get_channel_layer().group_send(site.channels_group_name(), {"type": "site.updated"})
        assert "Site unavailable" in await communicator.receive_from()
        assert (await communicator.receive_output())["type"] == "websocket.close"
        await communicator.disconnect()

    asyncio.run(check())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("requested", "expected"), (("settings", "settings"), ("unknown", "overview"))
)
def test_live_updates_preserve_validated_dashboard_tab(student, requested, expected):
    site = Site.objects.create(name="test-site", mode="static", purpose="project")
    site.users.add(student)

    async def check():
        communicator = communicator_for(site.id, student, f"?tab={requested}")
        assert (await communicator.connect())[0] is True
        marker = f'data-dashboard-tab="{expected}"'
        assert marker in await communicator.receive_from()
        await get_channel_layer().group_send(
            site.channels_group_name(), {"type": "operation.updated"}
        )
        assert marker in await communicator.receive_from()
        await communicator.disconnect()

    asyncio.run(check())
