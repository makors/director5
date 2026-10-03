"""Authorized runtime pages and CSRF-protected terminal/SSH access requests."""

import shlex
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .database_actions import database_runtime_options
from .models import Site
from .runtime_access import issue_access_token
from .runtime_client import RuntimeUnavailable, runtime_request
from .runtime_forms import TerminalSessionForm


def runtime_site(request, site_id, *, editable=True):
    if not request.user.is_active:
        return None
    queryset = (
        Site.objects.filter_editable(request.user)
        if editable
        else Site.objects.filter_visible(request.user)
    )
    return get_object_or_404(queryset, pk=site_id)


def terminal_options(site, kind):
    common = {"resource_limits": site.serialize_resource_limits()}
    if site.docker_image is not None:
        common["workspace_image"] = site.docker_image.name
    if kind == "database":
        if site.database is None or not site.database.provisioned:
            raise RuntimeUnavailable("This site has no provisioned database.")
        if getattr(site, "operation", None) and site.operation.ty in {
            "create_site_database",
            "delete_site_database",
            "regen_site_secrets",
        }:
            raise RuntimeUnavailable(
                "Wait for the database operation to finish before opening a terminal."
            )
        return {**common, **database_runtime_options(site.database)}
    environment = {"TZ": settings.TIME_ZONE}
    if site.database is not None and site.database.provisioned:
        database = site.database
        host = database.host
        hostname = (
            f"[{host.hostname}]"
            if ":" in host.hostname and not host.hostname.startswith("[")
            else host.hostname
        )
        url = f"{host.dbms}://{quote(database.username, safe='')}:{quote(database.password, safe='')}@{hostname}:{host.port}/{database.username}"
        environment.update(
            {
                "DATABASE_URL": url,
                "DIRECTOR_DATABASE_URL": url,
                "DIRECTOR_DATABASE_TYPE": host.dbms,
                "DIRECTOR_DATABASE_HOST": host.hostname,
                "DIRECTOR_DATABASE_PORT": str(host.port),
                "DIRECTOR_DATABASE_NAME": database.username,
                "DIRECTOR_DATABASE_USERNAME": database.username,
                "DIRECTOR_DATABASE_PASSWORD": database.password,
            }
        )
    return {**common, "kind": "shell", "environment": environment}


@login_required
def logs(request, site_id):
    site = runtime_site(request, site_id)
    if site is None:
        return HttpResponseForbidden()
    return render(request, "sites/runtime/logs.html", {"site": site})


@login_required
@never_cache
def logs_output(request, site_id):
    site = runtime_site(request, site_id)
    if site is None:
        return HttpResponseForbidden()
    context = {"site": site}
    try:
        result = runtime_request(site.pk, "logs")
        context.update(
            output=str(result.get("output", ""))[:65536], truncated=bool(result.get("truncated"))
        )
    except RuntimeUnavailable as error:
        context["error"] = str(error)
    return render(request, "sites/runtime/log_output.html", context)


@login_required
@never_cache
def runtime_status(request, site_id):
    site = runtime_site(request, site_id, editable=False)
    if site is None:
        return HttpResponseForbidden()
    try:
        result = runtime_request(site.pk, "status")
        return JsonResponse(result)
    except RuntimeUnavailable as error:
        return JsonResponse({"state": "unavailable", "message": str(error)}, status=503)


@login_required
def terminal(request, site_id):
    site = runtime_site(request, site_id)
    if site is None:
        return HttpResponseForbidden()
    kind = (
        "database"
        if request.GET.get("kind") == "database" or request.GET.get("sql") == "1"
        else "shell"
    )
    return render(request, "sites/runtime/terminal.html", {"site": site, "kind": kind})


@login_required
@require_POST
@never_cache
def terminal_session(request, site_id):
    site = runtime_site(request, site_id)
    if site is None:
        return HttpResponseForbidden()
    form = TerminalSessionForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"message": "Choose a supported terminal."}, status=400)
    kind = form.cleaned_data["kind"]
    try:
        terminal_options(site, kind)
    except RuntimeUnavailable as error:
        return JsonResponse({"message": str(error)}, status=409)
    token = issue_access_token(request.user, site, "terminal", kind)
    return JsonResponse({"token": token, "path": f"/sites/{site.pk}/terminal/"})


@login_required
@never_cache
def ssh_access(request, site_id):
    site = runtime_site(request, site_id)
    if site is None:
        return HttpResponseForbidden()
    host = settings.DIRECTOR_SSH_HOST
    port = settings.DIRECTOR_SSH_PORT
    command = f"ssh -p {port} {shlex.quote(f'site-{site.pk}@{host}')}"
    context = {"site": site, "command": command}
    if request.method == "POST":
        context["token"] = issue_access_token(request.user, site, "ssh")
    return render(request, "sites/runtime/ssh.html", context)


runtime_status.director_guidelines_api = True
terminal_session.director_guidelines_api = True
