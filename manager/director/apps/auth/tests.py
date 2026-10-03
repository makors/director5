"""Verify the Ion round trip, identity mapping, redirects, and sign-out."""

from html import unescape
from urllib.parse import parse_qs, quote, urlparse

import pytest
import responses
from django.contrib.auth import SESSION_KEY
from django.test import Client
from django.urls import reverse
from social_django.models import UserSocialAuth

from .oauth import IonOauth2, get_username


@pytest.fixture(autouse=True)
def ion_credentials(settings):
    settings.SOCIAL_AUTH_ION_KEY = "test-client"
    settings.SOCIAL_AUTH_ION_SECRET = "test-secret"


@pytest.fixture
def ion_profile():
    return {
        "id": 42100,
        "ion_username": "ion-student",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "tj_email": "ion-student@tjhsst.edu",
        "is_student": True,
        "is_teacher": False,
        "graduation_year": 2028,
        "is_staff": True,
        "is_superuser": True,
    }


def register_ion_responses(profile):
    responses.post(
        IonOauth2.ACCESS_TOKEN_URL,
        json={
            "access_token": "mock-access-token",
            "refresh_token": "mock-refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )
    responses.get("https://ion.tjhsst.edu/api/profile", json=profile)


def start_ion(client, next_url=""):
    response = client.get(reverse("social:begin", args=["ion"]), {"next": next_url})
    assert response.status_code == 302
    authorization = urlparse(response.url)
    assert authorization.scheme == "https"
    assert authorization.netloc == "ion.tjhsst.edu"
    assert authorization.path == "/oauth/authorize"
    params = parse_qs(authorization.query)
    assert params["scope"] == ["read"]
    assert params["client_id"] == ["test-client"]
    assert params["response_type"] == ["code"]
    assert params["state"] == [client.session["ion_state"]]
    assert urlparse(params["redirect_uri"][0]).path == reverse("social:complete", args=["ion"])
    return params["state"][0]


def test_login_preserves_safe_return_url(client):
    next_url = reverse("sites:create") + "?from=sign-in"
    response = client.get(reverse("auth:login"), {"next": next_url})
    assert response.status_code == 200
    assert response.context["next"] == next_url
    assert "?next=" + quote(next_url, safe="/") in response.content.decode()


@pytest.mark.parametrize(
    "next_url",
    (
        "https://elsewhere.example/",
        "//elsewhere.example/",
        "javascript:alert(1)",
        r"/\elsewhere.example/",
    ),
)
def test_login_rejects_unsafe_return_urls(client, student, next_url):
    response = client.get(reverse("auth:login"), {"next": next_url})
    assert response.context["next"] == ""
    client.force_login(student)
    response = client.get(reverse("auth:login"), {"next": next_url})
    assert response.url == reverse("sites:index")


def test_authenticated_login_returns_to_requested_page(client, student):
    client.force_login(student)
    response = client.get(reverse("auth:login"), {"next": reverse("sites:create")})
    assert response.url == reverse("sites:create")


def test_https_login_rejects_http_return_url(client, student):
    client.force_login(student)
    response = client.get(reverse("auth:login"), {"next": "http://testserver/create/"}, secure=True)
    assert response.url == reverse("sites:index")


@responses.activate
def test_ion_callback_creates_matching_identity_and_returns_to_site(
    client, django_user_model, ion_profile
):
    next_url = reverse("sites:create")
    state = start_ion(client, next_url)
    register_ion_responses(ion_profile)
    response = client.get(
        reverse("social:complete", args=["ion"]), {"code": "mock-code", "state": state}
    )
    assert response.status_code == 302
    assert response.url == next_url
    user = django_user_model.objects.get(username=ion_profile["ion_username"])
    assert user.id == ion_profile["id"]
    assert client.session[SESSION_KEY] == str(user.id)
    assert user.first_name == ion_profile["first_name"]
    assert user.email == ion_profile["tj_email"]
    assert user.is_student is True
    assert user.is_teacher is False
    assert user.graduation_year == ion_profile["graduation_year"]
    assert user.is_staff is False
    assert user.is_superuser is False
    association = UserSocialAuth.objects.get(user=user, provider="ion")
    assert association.uid == str(ion_profile["id"])
    assert association.extra_data["refresh_token"] == "mock-refresh-token"
    token_request = parse_qs(responses.calls[0].request.body)
    assert token_request["code"] == ["mock-code"]
    assert token_request["grant_type"] == ["authorization_code"]
    assert token_request["redirect_uri"] == ["http://testserver/social-auth/complete/ion/"]


@responses.activate
def test_repeat_ion_login_refreshes_profile_without_replacing_identity(
    client, django_user_model, ion_profile
):
    user = django_user_model.objects.create_user(
        id=ion_profile["id"],
        username=ion_profile["ion_username"],
        first_name="Old name",
        last_name="Old surname",
        email=ion_profile["tj_email"],
        is_student=True,
        graduation_year=2028,
    )
    UserSocialAuth.objects.create(user=user, provider="ion", uid=str(ion_profile["id"]))
    ion_profile.update(
        first_name="Updated", is_student=False, is_teacher=True, graduation_year=None
    )
    state = start_ion(client, reverse("sites:index"))
    register_ion_responses(ion_profile)
    response = client.get(
        reverse("social:complete", args=["ion"]), {"code": "mock-code", "state": state}
    )
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.first_name == "Updated"
    assert user.is_student is False
    assert user.is_teacher is True
    assert user.graduation_year is None
    assert user.id == ion_profile["id"]
    assert user.username == ion_profile["ion_username"]
    assert django_user_model.objects.count() == 1
    assert UserSocialAuth.objects.count() == 1


@pytest.mark.parametrize(
    "next_url", ("https://elsewhere.example/", "javascript:alert(1)", r"/\elsewhere.example/")
)
@responses.activate
def test_ion_callback_rejects_unsafe_return_urls(client, next_url, ion_profile):
    state = start_ion(client, next_url)
    assert client.session["next"] == reverse("sites:index")
    register_ion_responses(ion_profile)
    response = client.get(
        reverse("social:complete", args=["ion"]),
        {"code": "mock-code", "state": state, "next": next_url},
    )
    assert response.url == reverse("sites:index")


def test_cancelled_ion_sign_in_returns_safe_message(client):
    start_ion(client)
    response = client.get(
        reverse("social:complete", args=["ion"]),
        {"error": "access_denied", "error_description": "access_token=private-provider-value"},
        follow=True,
    )
    assert response.status_code == 200
    assert response.redirect_chain == [(reverse("auth:login"), 302)]
    assert "Ion sign-in was cancelled" in response.content.decode()
    assert "private-provider-value" not in response.content.decode()
    assert SESSION_KEY not in client.session


@responses.activate
def test_wrong_oauth_state_rejected_before_contacting_provider(client):
    start_ion(client)
    response = client.get(
        reverse("social:complete", args=["ion"]),
        {"code": "mock-code", "state": "wrong-state"},
        follow=True,
    )
    assert response.redirect_chain == [(reverse("auth:login"), 302)]
    assert "Your sign-in session expired" in response.content.decode()
    assert not responses.calls
    assert SESSION_KEY not in client.session


@responses.activate
def test_provider_failure_does_not_expose_token(client):
    state = start_ion(client)
    responses.post(IonOauth2.ACCESS_TOKEN_URL, json={"access_token": "private-provider-value"})
    responses.get("https://ion.tjhsst.edu/api/profile", status=503)
    response = client.get(
        reverse("social:complete", args=["ion"]), {"code": "mock-code", "state": state}, follow=True
    )
    assert response.redirect_chain == [(reverse("auth:login"), 302)]
    assert "We couldn't sign you in with Ion" in unescape(response.content.decode())
    assert "private-provider-value" not in response.content.decode()
    assert SESSION_KEY not in client.session


def test_username_pipeline_supports_existing_positional_user(student, monkeypatch):
    captured = {}

    def social_username(strategy, details, backend, user=None):
        captured["user"] = user
        return {"username": user.username}

    monkeypatch.setattr("director.apps.auth.oauth.social_get_username", social_username)
    assert get_username(None, {}, None, student) == {"username": student.username}
    assert captured["user"] == student


def test_logout_requires_post_and_csrf(student):
    client = Client(enforce_csrf_checks=True)
    client.force_login(student)
    assert client.get(reverse("auth:logout")).status_code == 405
    assert SESSION_KEY in client.session
    assert client.post(reverse("auth:logout")).status_code == 403
    assert SESSION_KEY in client.session
    client.get(reverse("sites:index"))
    response = client.post(
        reverse("auth:logout"), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value}
    )
    assert response.url == reverse("auth:login")
    assert SESSION_KEY not in client.session


def test_logout_of_expired_session_returns_to_login(client):
    response = client.post(reverse("auth:logout"))
    assert response.url == reverse("auth:login")
    assert SESSION_KEY not in client.session


def test_development_login_hidden_outside_debug(client, settings):
    settings.DEBUG = False
    response = client.get(reverse("auth:login"))
    assert "Development login" not in response.content.decode()


def test_ion_uses_public_https_callback_behind_trusted_proxy(client, settings):
    settings.ALLOWED_HOSTS = ["director.makors.xyz"]
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    settings.SESSION_COOKIE_SECURE = True
    response = client.get(
        reverse("social:begin", args=["ion"]),
        HTTP_HOST="director.makors.xyz",
        HTTP_X_FORWARDED_PROTO="https",
    )
    assert response.status_code == 302
    params = parse_qs(urlparse(response.url).query)
    assert params["redirect_uri"] == ["https://director.makors.xyz/social-auth/complete/ion/"]
    assert response.cookies[settings.SESSION_COOKIE_NAME]["secure"] is True
