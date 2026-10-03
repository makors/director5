"""Isolated UI preview: demo SQLite only; no mail, hosting, or worker delivery."""

from copy import deepcopy
from pathlib import Path

from director.settings import *  # noqa: F403

PREVIEW_ROOT = Path(__file__).resolve().parent
SECRET_KEY = "director-parity-isolated-preview-development-only"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": PREVIEW_ROOT / "preview.sqlite3"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
DEFAULT_FROM_EMAIL = "preview@example.test"
DIRECTOR_CONTACT_EMAIL = "preview-admin@example.test"
CONTACT_EMAIL = DIRECTOR_CONTACT_EMAIL
DIRECTOR_MANAGER_URL = "http://localhost:8083"
DIRECTOR_APPSERVER_HOSTS = []
DIRECTOR_APPSERVER_SSL = None
DIRECTOR_APPSERVER_TOKEN = ""
DIRECTOR_SSH_HOST = "unavailable.preview.invalid"
DIRECTOR_SSH_PORT = 2222
DIRECTOR_SSH_HOST_KEY = ""
DIRECTOR_REQUIRE_GUIDELINES = True
DIRECTOR_METRICS_SCRAPE_IPS = []
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_TASK_ALWAYS_EAGER = False
MEDIA_ROOT = PREVIEW_ROOT / "media"
STATIC_ROOT = PREVIEW_ROOT / "static"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
INSTALLED_APPS = [
    app for app in INSTALLED_APPS  # noqa: F405
    if app not in {"debug_toolbar", "django_browser_reload"}
] + ["parity_preview_apps.PreviewIsolation"]
MIDDLEWARE = [
    item for item in MIDDLEWARE  # noqa: F405
    if not item.startswith(("debug_toolbar.", "django_browser_reload."))
] + ["parity_preview_apps.PreviewNotice"]
TEMPLATES = deepcopy(TEMPLATES)  # noqa: F405
TEMPLATES[0]["APP_DIRS"] = False
TEMPLATES[0]["OPTIONS"]["loaders"] = [
    "django.template.loaders.filesystem.Loader", "django.template.loaders.app_directories.Loader"
]
