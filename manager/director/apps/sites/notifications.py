"""Private, best-effort mail notifications for persisted Director actions."""

import logging
from functools import wraps
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.urls import reverse

logger = logging.getLogger(__name__)


def _notification_boundary(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        record = args[0] if args else kwargs.get("site", kwargs.get("site_request"))
        try:
            return function(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 -- failed mail must not fail a persisted action.
            logger.warning(
                "Director notification %s for record %s failed (%s).",
                function.__name__,
                getattr(record, "pk", None),
                type(error).__name__,
            )
            return False

    return guarded


def manager_url(view_name: str, *args) -> str:
    """Build a review link from the configured public Manager URL."""
    origin = getattr(settings, "DIRECTOR_MANAGER_URL", "http://localhost:8080").strip()
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in origin
        or any(character.isspace() for character in origin)
    ):
        raise ValueError("Configure a valid public Manager URL.")
    # Accessing port also validates a malformed port without exposing the setting in logs.
    _ = parsed.port
    return urljoin(origin.rstrip("/") + "/", reverse(view_name, args=args).lstrip("/"))


def _send(*, record_id: int, recipient: str, subject: str, body: str) -> bool:
    recipient = recipient.strip()
    validate_email(recipient)
    contact = getattr(settings, "DIRECTOR_CONTACT_EMAIL", "").strip()
    validate_email(contact)
    message = EmailMessage(
        subject=subject,
        body=body,
        from_email=contact,
        to=[recipient],
        reply_to=[contact],
    )
    delivered = message.send(fail_silently=False) == 1
    if not delivered:
        logger.warning("Director notification for record %s was not delivered.", record_id)
    return delivered


@_notification_boundary
def notify_site_member_added(site, member, actor) -> bool:
    """Notify one newly added member after the membership transaction commits."""
    if (
        member.pk == actor.pk
        or not member.is_active
        or getattr(member, "is_service", False)
        or not site.users.filter(pk=member.pk, is_active=True).exists()
    ):
        return False
    return _send(
        record_id=site.pk,
        recipient=member.email,
        subject="You've been added to a website in Director",
        body=(
            f"{actor.username} added you to the website {site.name}.\n\n"
            f"Open the website in Director:\n{manager_url('sites:dashboard', site.pk)}\n"
        ),
    )


@_notification_boundary
def notify_site_created(site, actor) -> bool:
    """Notify new site members individually, excluding the creating actor."""
    results = [
        notify_site_member_added(site, member, actor)
        for member in site.users.filter(is_active=True).exclude(pk=actor.pk)
        if not getattr(member, "is_service", False)
    ]
    return bool(results) and all(results)


@_notification_boundary
def notify_site_request_submitted(site_request) -> bool:
    """Notify only the assigned teacher about a pending request."""
    teacher = site_request.teacher
    if (
        site_request.pk is None
        or site_request.teacher_approval is not None
        or site_request.admin_approval is not None
        or not teacher.is_active
        or not teacher.is_teacher
    ):
        return False
    return _send(
        record_id=site_request.pk,
        recipient=teacher.email,
        subject="A site request is ready for your review",
        body=(
            f"{site_request.user.username} requested a website for {site_request.activity}.\n\n"
            f"Review your assigned requests in Director:\n{manager_url('sites:request-teacher')}\n"
        ),
    )


@_notification_boundary
def notify_site_request_teacher_approved(site_request) -> bool:
    """Notify the configured Director contact after teacher approval."""
    if (
        site_request.pk is None
        or site_request.teacher_approval is not True
        or site_request.admin_approval is not None
    ):
        return False
    return _send(
        record_id=site_request.pk,
        recipient=getattr(settings, "DIRECTOR_CONTACT_EMAIL", ""),
        subject="A site request is ready for administrator review",
        body=(
            f"{site_request.teacher.username} approved {site_request.user.username}'s request "
            f"for {site_request.activity}.\n\n"
            f"Review approved requests in Director:\n{manager_url('sites:request-admin')}\n"
        ),
    )
