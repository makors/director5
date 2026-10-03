from __future__ import annotations

import logging
from functools import partial
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods
from django_htmx.http import HttpResponseClientRedirect

from .governance_forms import (
    AdminReviewForm,
    GuidelinesForm,
    SiteRequestForm,
    TeacherReviewForm,
)
from .models import SiteRequest
from .notifications import notify_site_request_submitted, notify_site_request_teacher_approved

logger = logging.getLogger(__name__)


def guidelines_api(view):
    """Mark an API view so guidelines failures are HTTP 401, not HTML redirects."""
    view.director_guidelines_api = True
    return view


def guidelines_exempt(view):
    view.director_guidelines_exempt = True
    return view


class GuidelinesMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        if not getattr(settings, "DIRECTOR_REQUIRE_GUIDELINES", True):
            return None
        if not request.user.is_authenticated or request.user.accepted_guidelines:
            return None
        if getattr(view_func, "director_guidelines_exempt", False):
            return None
        match = request.resolver_match
        namespace = match.namespace if match else ""
        protected = namespace.split(":", 1)[0] in {
            "sites",
            "marketplace",
            "admin",
        } or view_func.__module__.startswith("director.apps.sites.")
        if not protected:
            return None

        url = reverse("sites:guidelines")
        if (
            getattr(view_func, "director_guidelines_api", False)
            or "/api/" in request.path
            or "application/json" in request.headers.get("Accept", "").lower()
        ):
            return JsonResponse(
                {
                    "error": "Accept the website guidelines before continuing.",
                    "guidelines_url": url,
                },
                status=401,
            )
        target = f"{url}?{urlencode({'next': request.get_full_path()})}"
        if request.headers.get("HX-Request") == "true":
            return HttpResponseClientRedirect(target)
        return redirect(target)


def governance_context(request):
    user = request.user
    context = {
        "homepage_banner": getattr(settings, "HOMEPAGE_BANNER", ""),
        "is_graduating_soon": False,
        "pending_teacher_requests": 0,
        "pending_admin_requests": 0,
    }
    if not user.is_authenticated:
        return context
    context["is_graduating_soon"] = user.graduation_year == timezone.localdate().year
    if user.is_teacher:
        context["pending_teacher_requests"] = SiteRequest.objects.filter(
            teacher=user, teacher_approval__isnull=True
        ).count()
    if user.is_superuser:
        context["pending_admin_requests"] = SiteRequest.objects.filter(
            teacher_approval=True, admin_approval__isnull=True
        ).count()
    return context


def _safe_next(request):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return reverse("sites:index")


@guidelines_exempt
@login_required
@require_http_methods(["GET", "POST"])
def accept_guidelines(request):
    if request.user.accepted_guidelines:
        return redirect(_safe_next(request))
    form = GuidelinesForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        request.user.accepted_guidelines = True
        request.user.save(update_fields=["accepted_guidelines"])
        logger.info("Website guidelines accepted by user %s", request.user.pk)
        return redirect(_safe_next(request))
    return render(
        request,
        "sites/governance/guidelines.html",
        {"form": form, "next_url": _safe_next(request)},
        status=400 if request.method == "POST" else 200,
    )


@guidelines_exempt
@login_required
@require_GET
def read_guidelines(request):
    return render(request, "sites/governance/guidelines.html")


def _decision_labels(site_request):
    return {
        "request": site_request,
        "teacher_status": {None: "Pending", True: "Approved", False: "Rejected"}[
            site_request.teacher_approval
        ],
        "admin_status": {None: "Pending", True: "Processed", False: "Rejected"}[
            site_request.admin_approval
        ],
    }


def _posted_request_id(request):
    try:
        value = int(request.POST.get("request", ""))
    except (ValueError, TypeError):
        return None
    return value if 0 < value <= 2**63 - 1 else None


@login_required
@require_http_methods(["GET", "POST"])
def create_request(request):
    if not request.user.is_student:
        return HttpResponseForbidden("Only students can submit site requests.")
    form = SiteRequestForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        site_request = form.save(commit=False)
        site_request.user = request.user
        site_request.save()
        transaction.on_commit(partial(notify_site_request_submitted, site_request))
        logger.info("Site request %s submitted by user %s", site_request.pk, request.user.pk)
        messages.success(request, "Site request submitted for teacher review.")
        return redirect("sites:request-status")
    return render(
        request,
        "sites/governance/request_create.html",
        {"form": form},
        status=400 if request.method == "POST" else 200,
    )


@login_required
@require_GET
def request_status(request):
    if not request.user.is_student:
        return HttpResponseForbidden("Only students can view their site requests.")
    requests = SiteRequest.objects.filter(user=request.user).select_related("user", "teacher")
    return render(
        request,
        "sites/governance/request_status.html",
        {
            "requests": [
                _decision_labels(item) for item in requests.order_by("-request_date", "-id")
            ]
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def teacher_requests(request):
    if not request.user.is_teacher:
        return HttpResponseForbidden("Only teachers can review assigned site requests.")
    bound_form = None
    posted_id = None
    if request.method == "POST":
        posted_id = _posted_request_id(request)
        if posted_id is None:
            return HttpResponseBadRequest("Choose a valid site request.")
        with transaction.atomic():
            site_request = get_object_or_404(
                SiteRequest.objects.select_for_update().filter(teacher=request.user), pk=posted_id
            )
            if site_request.teacher_approval is not None:
                messages.error(request, "This request has already been reviewed by its teacher.")
                return redirect("sites:request-teacher")
            bound_form = TeacherReviewForm(request.POST, auto_id=f"request-{posted_id}-%s")
            if bound_form.is_valid():
                site_request.teacher_approval = bound_form.cleaned_data["action"] == "accept"
                site_request.save(update_fields=["teacher_approval"])
                if site_request.teacher_approval:
                    transaction.on_commit(
                        partial(notify_site_request_teacher_approved, site_request)
                    )
                logger.info(
                    "Site request %s teacher decision %s by user %s",
                    site_request.pk,
                    bound_form.cleaned_data["action"],
                    request.user.pk,
                )
                messages.success(
                    request,
                    "Request approved for administrator review."
                    if site_request.teacher_approval
                    else "Site request rejected.",
                )
                return redirect("sites:request-teacher")

    entries = []
    for item in (
        SiteRequest.objects.filter(teacher=request.user)
        .select_related("user", "teacher")
        .order_by("-request_date", "-id")
    ):
        entry = _decision_labels(item)
        if item.teacher_approval is None:
            entry["form"] = (
                bound_form
                if item.pk == posted_id
                else TeacherReviewForm(auto_id=f"request-{item.pk}-%s")
            )
        entries.append(entry)
    return render(
        request,
        "sites/governance/request_teacher.html",
        {"requests": entries},
        status=400 if bound_form is not None else 200,
    )


@login_required
@require_http_methods(["GET", "POST"])
def admin_requests(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only administrators can process site requests.")
    bound_form = None
    posted_id = None
    if request.method == "POST":
        posted_id = _posted_request_id(request)
        if posted_id is None:
            return HttpResponseBadRequest("Choose a valid site request.")
        with transaction.atomic():
            site_request = get_object_or_404(
                SiteRequest.objects.select_for_update().filter(teacher_approval=True), pk=posted_id
            )
            if site_request.admin_approval is True:
                messages.error(request, "This request has already been processed.")
                return redirect("sites:request-admin")
            bound_form = AdminReviewForm(request.POST, auto_id=f"request-{posted_id}-%s")
            if bound_form.is_valid():
                site_request.admin_approval = bound_form.cleaned_data["action"] == "accept"
                site_request.admin_comments = bound_form.cleaned_data["admin_comments"]
                site_request.private_admin_comments = bound_form.cleaned_data[
                    "private_admin_comments"
                ]
                site_request.save(
                    update_fields=["admin_approval", "admin_comments", "private_admin_comments"]
                )
                logger.info(
                    "Site request %s administrator decision %s by user %s",
                    site_request.pk,
                    bound_form.cleaned_data["action"],
                    request.user.pk,
                )
                messages.success(
                    request,
                    "Request marked as processed."
                    if site_request.admin_approval
                    else "Site request rejected.",
                )
                return redirect("sites:request-admin")

    entries = []
    for item in SiteRequest.objects.select_related("user", "teacher").order_by(
        "-request_date", "-id"
    ):
        entry = _decision_labels(item)
        if item.teacher_approval is True and item.admin_approval is not True:
            entry["form"] = (
                bound_form
                if item.pk == posted_id
                else AdminReviewForm(
                    initial={
                        "admin_comments": item.admin_comments,
                        "private_admin_comments": item.private_admin_comments,
                    },
                    auto_id=f"request-{item.pk}-%s",
                )
            )
        entries.append(entry)
    return render(
        request,
        "sites/governance/request_admin.html",
        {"requests": entries},
        status=400 if bound_form is not None else 200,
    )
