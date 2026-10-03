from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from ..users.models import MassEmail
from .mail_forms import ConfirmMassEmailForm, MassEmailForm

logger = logging.getLogger(__name__)


def _recipient_addresses(message):
    if message.status in {"sent", "failed"}:
        return list(message.recipient_emails)
    users = message.limit_users.all()
    if not users.exists():
        users = get_user_model().objects.all()
    addresses = set()
    for raw_email in users.values_list("email", flat=True):
        email = raw_email.strip()
        try:
            validate_email(email)
        except ValidationError:
            continue
        addresses.add(email)
    return sorted(addresses)


def _message_context(request, message, form=None):
    return {
        "message": message,
        "recipient_count": len(_recipient_addresses(message)),
        "can_send": message.sender_id == request.user.pk and message.status != "sent",
        "form": form
        if form is not None
        else ConfirmMassEmailForm(initial={"request_id": message.request_id}),
    }


@login_required
@require_http_methods(["GET", "POST"])
def mail_compose(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only administrators can compose mass emails.")
    form = MassEmailForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            message, created = MassEmail.objects.get_or_create(
                request_id=form.cleaned_data["request_id"],
                defaults={
                    "subject": form.cleaned_data["subject"],
                    "text_plain": form.cleaned_data["text_plain"],
                    "text_html": form.cleaned_data["text_html"],
                    "sender": request.user,
                },
            )
            if message.sender_id != request.user.pk:
                return HttpResponseForbidden("This draft belongs to another administrator.")
            if created:
                message.limit_users.set(form.cleaned_data["limit_users"])
        return redirect("sites:mail-detail", request_id=message.request_id)
    return render(
        request,
        "sites/mail/compose.html",
        {"form": form},
        status=400 if request.method == "POST" else 200,
    )


def _deliver_message(message, recipients):
    if not recipients:
        raise ValueError("No valid recipients")
    connection = get_connection(timeout=getattr(settings, "EMAIL_TIMEOUT", None) or 15)
    email = EmailMultiAlternatives(
        subject=message.subject,
        body=message.text_plain,
        from_email=settings.DEFAULT_FROM_EMAIL,
        bcc=recipients,
        connection=connection,
    )
    email.attach_alternative(message.text_html, "text/html")
    if email.send(fail_silently=False) != 1:
        raise RuntimeError("Mail backend did not confirm delivery")


@login_required
@require_http_methods(["GET", "POST"])
def mail_detail(request, request_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only administrators can view mass email history.")
    message = get_object_or_404(MassEmail.objects.select_related("sender"), request_id=request_id)
    if request.method != "POST":
        return render(request, "sites/mail/detail.html", _message_context(request, message))
    if message.sender_id != request.user.pk:
        return HttpResponseForbidden("Only the composing administrator can send this draft.")
    form = ConfirmMassEmailForm(request.POST)
    if form.is_valid() and form.cleaned_data["request_id"] != message.request_id:
        form.add_error("request_id", "The confirmation does not match this email.")
    if not form.is_valid():
        return render(
            request,
            "sites/mail/detail.html",
            _message_context(request, message, form),
            status=400,
        )

    with transaction.atomic():
        message = get_object_or_404(MassEmail.objects.select_for_update(), request_id=request_id)
        if message.sender_id != request.user.pk:
            return HttpResponseForbidden("Only the composing administrator can send this draft.")
        if message.status == "sent":
            messages.info(request, "This email has already been sent.")
            return redirect("sites:mail-detail", request_id=message.request_id)
        try:
            recipients = _recipient_addresses(message)
            message.recipient_emails = recipients
            message.save(update_fields=["recipient_emails"])
            _deliver_message(message, recipients)
        except Exception as error:  # noqa: BLE001 -- retain the draft when SMTP fails.
            message.status = "failed"
            message.sent_time = None
            message.save(update_fields=["status", "sent_time"])
            logger.warning("Mass email %s failed (%s)", message.request_id, type(error).__name__)
            messages.error(
                request,
                "Email delivery could not be confirmed.",
            )
        else:
            message.status = "sent"
            message.sent_time = timezone.now()
            message.save(update_fields=["status", "sent_time"])
            logger.info("Mass email %s sent by user %s", message.request_id, request.user.pk)
            messages.success(request, "Email sent.")
    return redirect("sites:mail-detail", request_id=message.request_id)


@login_required
@require_GET
def mail_history(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only administrators can view mass email history.")
    page = Paginator(
        MassEmail.objects.select_related("sender").order_by("-created_time", "-id"), 30
    ).get_page(request.GET.get("page"))
    return render(request, "sites/mail/history.html", {"page": page})
