"""Production settings; credentials come from the private deployment .env file."""

import os

DEBUG = False
SECRET_KEY = os.environ["DIRECTOR_SECRET_KEY"]
ALLOWED_HOSTS = ["director.makors.xyz"]
CSRF_TRUSTED_ORIGINS = ["https://director.makors.xyz"]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SOCIAL_AUTH_REDIRECT_IS_HTTPS = True
SOCIAL_AUTH_ION_KEY = os.environ.get("SOCIAL_AUTH_ION_KEY", "")
SOCIAL_AUTH_ION_SECRET = os.environ.get("SOCIAL_AUTH_ION_SECRET", "")
DIRECTOR_MANAGER_URL = "https://director.makors.xyz"
DIRECTOR_APPSERVER_HOSTS = ["orchestrator:8080"]
DIRECTOR_APPSERVER_TOKEN = os.environ["DIRECTOR_APPSERVER_TOKEN"]
DIRECTOR_CONTACT_EMAIL = "director@makors.xyz"
CONTACT_EMAIL = DIRECTOR_CONTACT_EMAIL
DIRECTOR_SSH_HOST = "director.makors.xyz"
DIRECTOR_SSH_PORT = 2222
DIRECTOR_SSH_HOST_KEY = "/run/director-ssh/ssh_host_ed25519_key"
SITE_URL_FORMATS = {None: "{}.sites.makors.xyz"}
DIRECTOR_RESOURCES_DEFAULT_CPUS = 0.6
DIRECTOR_RESOURCES_DEFAULT_MEMORY_LIMIT = 256 * 1024 * 1024
DIRECTOR_RESOURCES_MAX_REQUEST_BODY = 16 * 1024 * 1024

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "postgres",
        "PORT": "5432",
        "NAME": "director",
        "USER": "director",
        "PASSWORD": os.environ["DIRECTOR_POSTGRES_PASSWORD"],
        "CONN_MAX_AGE": 0,
    }
}
CELERY_BROKER_URL = "redis://redis:6379/0"
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://redis:6379/2",
    }
}
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": ["redis://redis:6379/1"]},
    }
}
STATIC_URL = "/static/"
STATIC_ROOT = "/static"
MEDIA_ROOT = "/media"
MEDIA_URL = "/media/"
DEFAULT_FROM_EMAIL = "director@makors.xyz"
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("DIRECTOR_SMTP_HOST", "")
EMAIL_PORT = int(os.environ.get("DIRECTOR_SMTP_PORT", "587"))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get("DIRECTOR_SMTP_USERNAME", "")
EMAIL_HOST_PASSWORD = os.environ.get("DIRECTOR_SMTP_PASSWORD", "")
EMAIL_TIMEOUT = 20
