"""Short-lived, single-use access tickets bound to a user and a site."""

import hashlib
import secrets

from django.core import signing
from django.core.cache import cache

LIFETIMES = {"terminal": 90, "ssh": 300}


def issue_access_token(user, site, purpose, kind="shell"):
    token = signing.dumps(
        {"user": user.pk, "site": site.pk, "kind": kind, "nonce": secrets.token_urlsafe(24)},
        salt=f"director.runtime.{purpose}",
        compress=False,
    )
    key = hashlib.sha256(token.encode()).hexdigest()
    cache.set(f"director-runtime-issued:{key}", value=True, timeout=LIFETIMES[purpose])
    return token


def load_access_token(token, purpose):
    if not isinstance(token, str) or len(token) > 4096:
        raise signing.BadSignature("Invalid access token")
    data = signing.loads(token, salt=f"director.runtime.{purpose}", max_age=LIFETIMES[purpose])
    if (
        not isinstance(data, dict)
        or type(data.get("user")) is not int
        or type(data.get("site")) is not int
    ):
        raise signing.BadSignature("Invalid access token")
    if data.get("kind") not in {"shell", "database"} or not isinstance(data.get("nonce"), str):
        raise signing.BadSignature("Invalid access token")
    return data


def consume_access_token(token, purpose, user_id, site_id):
    data = load_access_token(token, purpose)
    if data["user"] != user_id or data["site"] != site_id:
        raise signing.BadSignature("Access token does not match this site and user")
    key = hashlib.sha256(token.encode()).hexdigest()
    if not cache.get(f"director-runtime-issued:{key}"):
        raise signing.BadSignature("Access token was not issued or has expired")
    if not cache.add(f"director-runtime-used:{key}", value=True, timeout=LIFETIMES[purpose] + 5):
        raise signing.BadSignature("Access token was already used")
    cache.delete(f"director-runtime-issued:{key}")
    return data
