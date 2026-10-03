import secrets

from . import settings


def credential_valid(authorization: str | None) -> bool:
    """Authenticate Manager requests without logging the shared secret."""
    if not settings.APPSERVER_TOKEN:
        return settings.DEBUG
    return secrets.compare_digest(
        (authorization or "").encode(), f"Bearer {settings.APPSERVER_TOKEN}".encode()
    )
