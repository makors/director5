"""Live dashboard updates for authorized site members."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.http import QueryDict
from django.template.loader import render_to_string
from django.urls import path, reverse
from django.utils.html import format_html

from ..users.models import User as UserModel
from .models import Site
from .status import normalize_dashboard_tab, status_context

if TYPE_CHECKING:
    from ..users.models import User


@database_sync_to_async
def find_site(site_id: int, user: User) -> Site:
    user = UserModel.objects.filter(pk=user.pk, is_active=True).first()
    if user is None or (
        getattr(settings, "DIRECTOR_REQUIRE_GUIDELINES", True) and not user.accepted_guidelines
    ):
        raise Site.DoesNotExist
    return Site.objects.filter_visible(user).get(id=site_id)


class SiteInfoConsumer(AsyncWebsocketConsumer):
    """Send the same escaped status fragment as the initial dashboard."""

    @override
    async def connect(self):
        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close(code=4401)
            return
        if getattr(settings, "DIRECTOR_REQUIRE_GUIDELINES", True) and not user.accepted_guidelines:
            await self.close(code=4401)
            return
        try:
            site_id = int(self.scope["url_route"]["kwargs"]["site_id"])
            self.site = await find_site(site_id, user)
        except (Site.DoesNotExist, ValueError, KeyError):
            await self.close(code=4404)
            return

        self.dashboard_tab = normalize_dashboard_tab(
            QueryDict(self.scope.get("query_string", b"")).get("tab")
        )

        self.group_name = self.site.channels_group_name()
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_site_info()

    @override
    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def operation_updated(self, event: dict[str, Any]) -> None:
        await self.send_site_info()

    async def site_updated(self, event: dict[str, Any]) -> None:
        await self.send_site_info()

    async def site_deleted(self, event: dict[str, Any]) -> None:
        await self.send_unavailable()

    async def send_site_info(self) -> None:
        try:
            html = await self.render_site_info()
        except Site.DoesNotExist:
            await self.send_unavailable()
            return
        await self.send(text_data=html)

    @database_sync_to_async
    def render_site_info(self) -> str:
        # Re-check access on every update so revoked membership takes effect.
        user = UserModel.objects.filter(pk=self.scope["user"].pk, is_active=True).first()
        if user is None or (
            getattr(settings, "DIRECTOR_REQUIRE_GUIDELINES", True) and not user.accepted_guidelines
        ):
            raise Site.DoesNotExist
        self.site = Site.objects.filter_visible(user).get(pk=self.site.pk)
        context = status_context(self.site, user, dashboard_tab=self.dashboard_tab)
        if settings.CSRF_USE_SESSIONS:
            context["csrf_token"] = self.scope["session"].get("_csrftoken", "")
        else:
            context["csrf_token"] = self.scope.get("cookies", {}).get(settings.CSRF_COOKIE_NAME, "")
        return render_to_string("sites/partials/site_status.html", context)

    async def send_unavailable(self) -> None:
        await self.send(
            text_data=str(
                format_html(
                    '<section id="site-status" hx-swap-oob="true" class="dt-panel dt-empty-state">'
                    "<h2>Site unavailable</h2><p>This site was removed or you no longer have access.</p>"
                    '<a class="dt-btn-primary" href="{}">Back to sites</a></section>',
                    reverse("sites:index"),
                )
            )
        )
        await self.close(code=4404)


urlpatterns = [
    path("sites/<int:site_id>/", SiteInfoConsumer.as_asgi()),
]
