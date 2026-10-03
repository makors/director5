from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_htmx.http import HttpResponseClientRedirect

from . import tasks
from .forms import CreateSiteForm
from .models import Action, Operation, OperationInProgressError, Site
from .notifications import notify_site_created
from .operations import send_operation_updated_message
from .search import search_sites
from .status import normalize_dashboard_tab, status_context

if TYPE_CHECKING:
    from director.djtypes import AuthenticatedHttpRequest

logger = logging.getLogger(__name__)


@login_required
def index(request: AuthenticatedHttpRequest) -> HttpResponse:
    sites = (
        Site.objects.filter_visible(request.user)
        .select_related("operation")
        .prefetch_related("operation__action_set")
    )
    total_sites = sites.count()
    q = request.GET.get("q", "").strip()
    mode = request.GET.get("mode", "")
    if q:
        sites = search_sites(sites, q)
    show_all = request.user.is_superuser and bool(
        request.GET.get("all") == "1" or (q and not sites.filter(users=request.user).exists())
    )
    if request.user.is_superuser and not show_all:
        sites = sites.filter(users=request.user)
    membership = Site.users.through.objects.filter(site_id=OuterRef("pk"), user_id=request.user.pk)
    sites = sites.annotate(owned_by_current_user=Exists(membership))
    if mode in {"static", "dynamic"}:
        sites = sites.filter(mode=mode)
    else:
        mode = ""

    page = Paginator(sites.order_by("-owned_by_current_user", "name"), 30).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "sites/index.html",
        {
            "sites": page.object_list,
            "page_obj": page,
            "q": q,
            "mode": mode,
            "total_sites": total_sites,
            "result_count": page.paginator.count,
            "show_all": show_all,
        },
    )


@login_required
def site_dashboard(request: AuthenticatedHttpRequest, site_id: int) -> HttpResponse:
    site = get_object_or_404(Site.objects.filter_visible(request.user), id=site_id)
    return render(
        request,
        "sites/dashboard.html",
        status_context(site, request.user, dashboard_tab=request.GET.get("tab", "overview")),
    )


def _dashboard_url(request: AuthenticatedHttpRequest, site: Site) -> str:
    url = reverse("sites:dashboard", args=[site.id])
    if normalize_dashboard_tab(request.POST.get("tab")) == "settings":
        return url + "?tab=settings"
    return url


def _redirect(request: AuthenticatedHttpRequest, url: str) -> HttpResponse:
    if request.htmx:
        return HttpResponseClientRedirect(url)
    return redirect(url)


def _queue_operation(site: Site, operation: Operation, task) -> bool:
    """Keep queue failures visible and retryable on the dashboard."""
    try:
        task.delay(operation.id)
    except Exception:
        logger.exception("Could not queue operation %s for site %s", operation.id, site.id)
        Action.objects.create(
            operation=operation,
            slug="queue_operation",
            name="Queueing operation",
            result=False,
            user_message="The hosting service is unavailable. Please try again in a moment.",
        )
        send_operation_updated_message(site)
        return False
    return True


@login_required
def create_site(request: AuthenticatedHttpRequest, *, personal=False) -> HttpResponse:
    if personal:
        existing = Site.objects.filter(users=request.user, purpose="user").first()
        if existing:
            return _redirect(request, reverse("sites:dashboard", args=[existing.pk]))
    if request.method == "POST":
        form = CreateSiteForm(request.POST, user=request.user, personal=personal)
        if form.is_valid():
            with transaction.atomic():
                site = form.save()
                site.users.add(request.user)
                op = site.start_operation("create_site")
                transaction.on_commit(partial(notify_site_created, site, request.user))
            if _queue_operation(site, op, tasks.create_site):
                messages.success(request, "Your site is being created.")
            else:
                messages.error(
                    request, "Your site was saved, but setup could not start. Use Retry below."
                )
            return _redirect(request, reverse("sites:dashboard", args=[site.id]))

        if request.htmx:
            return render(request, "sites/partials/create_form.html", {"form": form})

    else:
        form = CreateSiteForm(user=request.user, personal=personal)

    return render(request, "sites/create.html", {"form": form})


@login_required
@require_POST
def delete_site(request: AuthenticatedHttpRequest, site_id: int) -> HttpResponse:
    site = get_object_or_404(Site.objects.filter_editable(request.user), id=site_id)
    if request.POST.get("confirmation") != site.name:
        messages.error(request, "Enter the site name to confirm deletion.")
        return _redirect(request, _dashboard_url(request, site))
    return _perform_operation(request, site, "delete_site", tasks.delete_site, replace_failed=True)


def _perform_operation(  # noqa: PLR0913 -- bind retries to the operation the user confirmed.
    request, site, kind, task, *, replace_failed=False, expected_operation_id=None
):
    try:
        operation = site.start_operation(
            kind, replace_failed=replace_failed, expected_operation_id=expected_operation_id
        )
    except OperationInProgressError:
        messages.error(
            request,
            "An operation is already in progress. Wait for it to finish or retry a failed operation.",
        )
    else:
        if _queue_operation(site, operation, task):
            if kind == "delete_site":
                messages.success(
                    request, "Deletion requested. The site will disappear when cleanup finishes."
                )
                return _redirect(request, reverse("sites:index"))
            messages.success(request, f"{operation.kind_label}. You can follow its progress below.")
            return _redirect(request, reverse("sites:dashboard", args=[site.id]))
        messages.error(request, "The operation could not start. Please use Retry below.")
    return _redirect(request, _dashboard_url(request, site))


@login_required
@require_POST
def restart_site(request: AuthenticatedHttpRequest, site_id: int) -> HttpResponse:
    site = get_object_or_404(Site.objects.filter_editable(request.user), id=site_id)
    return _perform_operation(request, site, "restart_site", tasks.restart_site_process)


@login_required
@require_POST
def rebuild_site(request: AuthenticatedHttpRequest, site_id: int) -> HttpResponse:
    site = get_object_or_404(Site.objects.filter_editable(request.user), id=site_id)
    return _perform_operation(request, site, "fix_site", tasks.rebuild_docker_image)


@login_required
@require_POST
def retry_operation(
    request: AuthenticatedHttpRequest, site_id: int, *, expected_operation_id: int | None = None
) -> HttpResponse:
    from .database_tasks import RETRY_TASKS
    from .images_tasks import apply_resource_limits, rebuild_image
    from .management_views import apply_availability
    from .settings_tasks import apply_site_settings

    site = get_object_or_404(Site.objects.filter_editable(request.user), id=site_id)
    operation = Operation.objects.filter(site=site).first()
    retry_tasks = {
        "create_site": tasks.create_site,
        "restart_site": tasks.restart_site_process,
        "fix_site": tasks.rebuild_docker_image,
        "update_docker_image": rebuild_image,
        "update_resource_limits": apply_resource_limits,
        "update_availability": apply_availability,
        "delete_site": tasks.delete_site,
    }
    retry_tasks.update(RETRY_TASKS)
    retry_tasks.update(
        dict.fromkeys(
            ["rename_site", "edit_site_names", "change_site_type", "regen_nginx_config"],
            apply_site_settings,
        )
    )
    if (
        not operation
        or operation.status != "failed"
        or operation.ty not in retry_tasks
        or (expected_operation_id is not None and operation.pk != expected_operation_id)
    ):
        messages.error(request, "There is no failed operation to retry.")
        return _redirect(request, _dashboard_url(request, site))
    return _perform_operation(
        request,
        site,
        operation.ty,
        retry_tasks[operation.ty],
        replace_failed=True,
        expected_operation_id=operation.pk,
    )
