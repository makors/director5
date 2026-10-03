"""Authorized database lifecycle and SQL-console pages."""

import secrets
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.http import require_POST
from requests import RequestException

from . import database_tasks
from .actions import raise_by_recoverability
from .appserver import Appserver
from .database_actions import database_query_payload
from .database_forms import CreateDatabaseForm, QueryDatabaseForm
from .models import Database, Operation, OperationInProgressError, Site
from .operations import UserFacingError
from .views import _queue_operation


def _editable_site(request, site_id):
    return get_object_or_404(
        Site.objects.filter_editable(request.user).select_related("database__host"),
        pk=site_id,
    )


def _operation(site):
    return Operation.objects.filter(site=site).first()


def _database_context(site):
    database = site.database
    context = {"site": site, "database": database, "operation": _operation(site)}
    if database:
        hostname = database.host.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        context["connection_url"] = (
            f"{database.host.dbms}://{database.username}:{quote(database.password, safe='')}"
            f"@{hostname}:{database.host.port}/{database.username}"
        )
        try:
            context["database_terminal_url"] = (
                reverse("sites:terminal", args=[site.id]) + "?kind=database"
            )
        except NoReverseMatch:
            # Allows this database module to be mounted independently of runtime tooling.
            pass
    return context


def _dashboard(site):
    return reverse("sites:dashboard", args=[site.id])


def _detail(site):
    return reverse("databases:detail", args=[site.id])


@login_required
def database_detail(request, site_id) -> HttpResponse:
    site = _editable_site(request, site_id)
    response = render(request, "sites/databases/detail.html", _database_context(site))
    response["Cache-Control"] = "no-store"
    return response


@login_required
def create_database(request, site_id) -> HttpResponse:
    site = _editable_site(request, site_id)
    if site.database_id:
        return redirect(_detail(site))
    if site.mode != "dynamic":
        messages.error(request, "Databases can only be added to dynamic sites.")
        return redirect(_dashboard(site))
    form = CreateDatabaseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                site = Site.objects.select_for_update().get(pk=site.pk)
                if site.database_id:
                    raise OperationInProgressError("This site already has a database.")
                operation = site.start_operation("create_site_database")
                database = Database.objects.create(
                    host=form.cleaned_data["host"],
                    password=secrets.token_urlsafe(32),
                    provisioned=False,
                )
                site.database = database
                site.save(update_fields=["database"])
        except OperationInProgressError:
            form.add_error(None, "An operation is already in progress on this site.")
        else:
            if _queue_operation(site, operation, database_tasks.create_database):
                messages.success(
                    request, "Database creation requested. Follow its progress on Overview."
                )
                return redirect(_dashboard(site))
            messages.error(
                request, "Database setup could not start. Retry the failed operation on Overview."
            )
            return redirect(_detail(site))
    return render(request, "sites/databases/create.html", {"site": site, "form": form})


@login_required
@require_POST
def delete_database(request, site_id) -> HttpResponse:
    site = _editable_site(request, site_id)
    if site.database is None:
        return redirect(_detail(site))
    if request.POST.get("confirmation") != site.name:
        messages.error(request, "Enter the site name to confirm database deletion.")
        return redirect(_detail(site))
    return _start_database_operation(
        request, site, "delete_site_database", database_tasks.delete_database
    )


@login_required
@require_POST
def rotate_database_password(request, site_id) -> HttpResponse:
    site = _editable_site(request, site_id)
    if site.database is None or not site.database.provisioned:
        messages.error(request, "The database is not ready yet.")
        return redirect(_detail(site))
    try:
        with transaction.atomic():
            site = Site.objects.select_for_update().get(pk=site.pk)
            operation = site.start_operation("regen_site_secrets")
            database = site.database
            database.pending_password = secrets.token_urlsafe(32)
            database.save(update_fields=["pending_password"])
    except OperationInProgressError:
        messages.error(request, "An operation is already in progress on this site.")
        return redirect(_detail(site))
    if _queue_operation(site, operation, database_tasks.rotate_database_password):
        messages.success(
            request,
            "Password rotation requested. Applications will restart with the new connection.",
        )
        return redirect(_dashboard(site))
    messages.error(
        request, "Password rotation could not start. Retry the failed operation on Overview."
    )
    return redirect(_detail(site))


def _start_database_operation(request, site, kind, task):
    try:
        operation = site.start_operation(kind)
    except OperationInProgressError:
        messages.error(request, "An operation is already in progress on this site.")
        return redirect(_detail(site))
    if _queue_operation(site, operation, task):
        messages.success(request, "Database deletion requested. Follow its progress on Overview.")
        return redirect(_dashboard(site))
    messages.error(
        request, "Database deletion could not start. Retry the failed operation on Overview."
    )
    return redirect(_detail(site))


@login_required
def query_database(request, site_id) -> HttpResponse:
    site = _editable_site(request, site_id)
    database = site.database
    if database is None or not database.provisioned:
        messages.error(request, "The database is not ready yet.")
        return redirect(_detail(site))
    operation = _operation(site)
    form = QueryDatabaseForm(request.POST or None, initial={"sql": "SELECT 1;"})
    result = None
    if request.method == "POST" and form.is_valid():
        if operation:
            form.add_error(
                None, "Wait for the current site operation to finish before running queries."
            )
        else:
            try:
                appserver = Appserver.list_pingable()[0]
                response = appserver.http_request(
                    "/api/database/query",
                    method="POST",
                    data=database_query_payload(database, form.cleaned_data["sql"]),
                )
                raise_by_recoverability(site, response)
                query_result = response.json()
                if (
                    not isinstance(query_result, dict)
                    or not isinstance(query_result.get("columns"), list)
                    or not isinstance(query_result.get("rows"), list)
                    or not isinstance(query_result.get("affected_rows"), int)
                ):
                    raise ValueError("The database server did not return a query result.")
                result = query_result
            except UserFacingError as error:
                form.add_error(None, str(error))
            except (RequestException, RuntimeError, ValueError, IndexError):
                form.add_error(None, "The database server could not be reached. Please try again.")
    response = render(
        request,
        "sites/databases/query.html",
        {
            "site": site,
            "database": database,
            "form": form,
            "result": result,
            "operation": operation,
        },
    )
    response["Cache-Control"] = "no-store"
    return response
