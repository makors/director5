from typing import Any, cast

from django.utils.http import url_has_allowed_host_and_scheme
from social_core.backends.base import BaseAuth
from social_core.backends.oauth import BaseOAuth2
from social_core.pipeline.user import get_username as social_get_username
from social_core.strategy import BaseStrategy


def get_username(
    strategy: BaseStrategy,
    details: dict[str, Any],
    backend: BaseAuth,
    user=None,
    *args: Any,
    **kwargs: Any,
) -> dict[str, str] | None:
    return social_get_username(strategy, details, backend, user, *args, **kwargs)


def attach_pending_sites(user=None, **kwargs: Any) -> None:
    if user is None:
        return
    from django.db import transaction

    from director.apps.sites.models import SitePendingUser

    with transaction.atomic():
        pending = SitePendingUser.objects.select_for_update().filter(username=user.username).first()
        if pending:
            for site in pending.sites.all():
                site.users.add(user)
            pending.delete()


def sync_graduation_year(
    strategy: BaseStrategy, details: dict[str, Any], user=None, **kwargs: Any
) -> None:
    """Also clear an old year when Ion returns a nullable graduation year."""
    if (
        user is not None
        and "graduation_year" in details
        and user.graduation_year != details["graduation_year"]
    ):
        user.graduation_year = details["graduation_year"]
        strategy.storage.user.changed(user)


class IonOauth2(BaseOAuth2):
    name = "ion"
    # OAuth2 already carries state separately; keep the registered callback exact.
    REDIRECT_STATE = False
    AUTHORIZATION_URL = "https://ion.tjhsst.edu/oauth/authorize"
    ACCESS_TOKEN_URL = "https://ion.tjhsst.edu/oauth/token"
    ACCESS_TOKEN_METHOD = "POST"
    EXTRA_DATA = [("refresh_token", "refresh_token", True), ("expires_in", "expires")]

    def _sanitize_next(self) -> None:
        """Keep return URLs on this host throughout the OAuth round trip."""
        next_url = self.strategy.session_get("next") or self.data.get("next", "")
        if not url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={self.strategy.request_host()},
            require_https=self.strategy.request_is_secure(),
        ):
            next_url = self.setting("LOGIN_REDIRECT_URL", "/")
        self.strategy.session_set("next", next_url)

    def start(self):
        self._sanitize_next()
        return super().start()

    def auth_complete(self, *args: Any, **kwargs: Any):
        self._sanitize_next()
        return super().auth_complete(*args, **kwargs)

    def get_scope(self) -> list[str]:
        return ["read"]

    def get_user_details(self, response: dict[str, Any]) -> dict[str, Any]:
        profile = self.get_json(
            "https://ion.tjhsst.edu/api/profile", params={"access_token": response["access_token"]}
        )

        # fields used to populate/update User model
        data = {
            key: profile[key]
            for key in (
                "first_name",
                "last_name",
                "id",
                "is_student",
                "is_teacher",
                "graduation_year",
            )
        }
        data["username"] = profile["ion_username"]
        data["email"] = profile["tj_email"]
        return data

    def get_user_id(self, details: dict[str, Any], response: Any) -> int:
        return cast(int, details["id"])
