"""Deployment settings must override development defaults before Django starts."""

import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_settings(monkeypatch, **overrides):
    secret = ModuleType("director.settings.secret")
    secret.__dict__.update(overrides)
    monkeypatch.setitem(sys.modules, secret.__name__, secret)
    monkeypatch.delenv("PYTEST_VERSION", raising=False)
    monkeypatch.delenv("CI", raising=False)
    settings_path = Path(__file__).with_name("__init__.py")
    namespace = {"__file__": str(settings_path), "__package__": "director.settings"}
    exec(compile(settings_path.read_text(), str(settings_path), "exec"), namespace)
    return namespace


def test_production_overrides_survive_all_defaults(monkeypatch):
    database = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": "private-postgres",
            "NAME": "production",
            "USER": "production",
            "PASSWORD": "test-production-password",
        }
    }
    overrides = {
        "DEBUG": False,
        "SECRET_KEY": "test-production-key",
        "DATABASES": database,
        "SITE_URL_FORMATS": {None: "{}.sites.example.org"},
        "DIRECTOR_APPSERVER_HOSTS": ["private-orchestrator:8080"],
        "CELERY_BROKER_URL": "redis://private-redis:6379/0",
        "CHANNEL_LAYERS": {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
        "MEDIA_ROOT": Path("/srv/director/site-files"),
    }
    production = load_settings(monkeypatch, **overrides)
    for setting, value in overrides.items():
        assert production[setting] == value
    assert production["AUTHENTICATION_BACKENDS"] == ["director.apps.auth.oauth.IonOauth2"]
    assert not {"debug_toolbar", "django_browser_reload", "django_extensions"}.intersection(
        production["INSTALLED_APPS"]
    )
    assert not any(
        "debug_toolbar" in middleware or "django_browser_reload" in middleware
        for middleware in production["MIDDLEWARE"]
    )


def test_secret_import_errors_fail_startup(monkeypatch):
    class BrokenSecret(ModuleType):
        def __getattr__(self, name):
            raise ModuleNotFoundError("Missing production dependency", name="missing_dependency")

    secret = BrokenSecret("director.settings.secret")
    monkeypatch.setitem(sys.modules, secret.__name__, secret)
    settings_path = Path(__file__).with_name("__init__.py")
    namespace = {"__file__": str(settings_path), "__package__": "director.settings"}
    with pytest.raises(ModuleNotFoundError, match="Missing production dependency"):
        exec(compile(settings_path.read_text(), str(settings_path), "exec"), namespace)


def test_production_django_boots_without_development_routes():
    script = """
import os
import sys
from types import ModuleType

secret = ModuleType("director.settings.secret")
secret.DEBUG = False
sys.modules[secret.__name__] = secret
os.environ["DJANGO_SETTINGS_MODULE"] = "director.settings"

import django
django.setup()
from django.test import Client
from django.urls import Resolver404, resolve

assert resolve("/accounts/login/").url_name == "login"
assert resolve("/social-auth/complete/ion/").url_name == "complete"
response = Client().get("/accounts/login/", HTTP_HOST="localhost")
assert response.status_code == 200
assert b"Development login" not in response.content
for path in ("/accounts/password-login/", "/__reload__/", "/components.html"):
    try:
        resolve(path)
    except Resolver404:
        continue
    raise AssertionError(f"Production exposed development route {path}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
