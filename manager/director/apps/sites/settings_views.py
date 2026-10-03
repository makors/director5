"""Permission-checked site settings and access management."""

from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import settings_tasks
from .models import Domain, Operation, OperationInProgressError, Site
from .notifications import notify_site_member_added
from .operations import send_site_updated_message
from .settings_forms import (
    AddDomainForm,
    AddMemberForm,
    SiteAdminForm,
    SiteMetaForm,
    SiteNameForm,
    SiteTypeForm,
)
from .views import _queue_operation, _redirect

FORMS = {
    "meta": ("sites:settings-meta", "Save changes"),
    "name": ("sites:settings-name", "Rename and deploy"),
    "type": ("sites:settings-type", "Save and deploy"),
    "admin": ("sites:settings-admin", "Save changes"),
    "member": ("sites:settings-member-add", "Add member"),
    "domain": ("sites:settings-domain-add", "Add domain"),
}
ROUTING_OPERATIONS = {"rename_site", "edit_site_names", "regen_nginx_config"}


def _can_deploy(operation, kind):
    if operation is None:
        return True
    if operation.status != "failed":
        return False
    if kind in ROUTING_OPERATIONS:
        return operation.ty in ROUTING_OPERATIONS
    return kind == operation.ty == "change_site_type"


def _start_settings_operation(site, kind):
    current = Operation.objects.filter(site=site).first()
    return site.start_operation(kind, replace_failed=bool(current and _can_deploy(current, kind)))


def _settings_url(site: Site) -> str:
    return reverse("sites:settings", args=[site.pk])


def _editable_site(request, site_id, *, lock=False):
    queryset = Site.objects.filter_editable(request.user)
    if lock:
        queryset = queryset.select_for_update()
    return get_object_or_404(queryset, pk=site_id)


def _context(request, site: Site, **forms) -> dict:
    # ModelForm validation modifies its instance even on failure. Keep the page
    # header and other forms grounded in the saved state, while retaining bound data.
    site = get_object_or_404(Site.objects.filter_visible(request.user), pk=site.pk)
    can_edit = request.user.is_superuser or site.availability in {"enabled", "not-served"}
    operation = Operation.objects.filter(site=site).first()
    context = {
        "site": site,
        "can_edit": can_edit,
        "operation": operation,
        "can_change_routing": _can_deploy(operation, "rename_site"),
        "can_change_type": _can_deploy(operation, "change_site_type"),
        "members": list(site.users.order_by("username")),
        "domains": list(site.domain_set.exclude(status="deleted").order_by("domain")),
        "domain_target": urlsplit(site.sites_url).hostname,
        "meta_form": SiteMetaForm(instance=site, user=request.user),
        "name_form": SiteNameForm(instance=site, user=request.user),
        "type_form": SiteTypeForm(instance=site, user=request.user),
        "member_form": AddMemberForm(site=site),
        "domain_form": AddDomainForm(site=site, user=request.user),
    }
    if request.user.is_superuser:
        context["admin_form"] = SiteAdminForm(instance=site, user=request.user)
    context.update(forms)
    return context


def _invalid(request, site, section, form):
    if request.htmx:
        action_name, submit_label = FORMS[section]
        operation = Operation.objects.filter(site=site).first()
        return render(
            request,
            "sites/settings_form.html",
            {
                "site": Site.objects.get(pk=site.pk),
                "form": form,
                "section": section,
                "action_name": action_name,
                "submit_label": submit_label,
                "operation": operation,
                "can_change_routing": _can_deploy(operation, "rename_site"),
                "can_change_type": _can_deploy(operation, "change_site_type"),
            },
        )
    return render(
        request, "sites/settings.html", _context(request, site, **{section + "_form": form})
    )


def _deployment_saved(request, site, operation):
    if _queue_operation(site, operation, settings_tasks.apply_site_settings):
        messages.success(request, "Changes saved. Deployment is queued.")
    else:
        messages.error(
            request, "Changes saved, but deployment could not start. Retry it from Overview."
        )
    return _redirect(request, _settings_url(site))


def _save_model_form(request, site_id, form_class, section, *, deployment):
    deployment_field, operation_kind = deployment
    form = None
    site = None
    operation = None
    try:
        with transaction.atomic():
            site = _editable_site(request, site_id, lock=True)
            form = form_class(request.POST, instance=site, user=request.user)
            if not form.is_valid():
                return _invalid(request, site, section, form)
            if deployment_field in form.changed_data:
                operation = _start_settings_operation(site, operation_kind)
            if form.changed_data:
                form.save()
                transaction.on_commit(lambda: send_site_updated_message(site))
    except OperationInProgressError:
        form.add_error(
            None, "Finish or retry the current operation before changing deployment settings."
        )
        return _invalid(request, site, section, form)
    except IntegrityError:
        form.add_error(
            None, "This change conflicts with another site. Reload the page and try again."
        )
        return _invalid(request, site, section, form)

    if operation:
        return _deployment_saved(request, site, operation)
    messages.success(request, "Changes saved.")
    return _redirect(request, _settings_url(site))


@login_required
def settings_view(request, site_id):
    site = get_object_or_404(Site.objects.filter_visible(request.user), pk=site_id)
    return render(request, "sites/settings.html", _context(request, site))


@login_required
@require_POST
def edit_meta(request, site_id):
    return _save_model_form(
        request, site_id, SiteMetaForm, "meta", deployment=("purpose", "edit_site_names")
    )


@login_required
@require_POST
def edit_name(request, site_id):
    return _save_model_form(
        request, site_id, SiteNameForm, "name", deployment=("name", "rename_site")
    )


@login_required
@require_POST
def edit_type(request, site_id):
    return _save_model_form(
        request, site_id, SiteTypeForm, "type", deployment=("mode", "change_site_type")
    )


@login_required
@require_POST
def edit_admin(request, site_id):
    if not request.user.is_superuser:
        raise PermissionDenied
    return _save_model_form(
        request,
        site_id,
        SiteAdminForm,
        "admin",
        deployment=("custom_nginx_config", "regen_nginx_config"),
    )


@login_required
@require_POST
def add_member(request, site_id):
    with transaction.atomic():
        site = _editable_site(request, site_id, lock=True)
        form = AddMemberForm(request.POST, site=site)
        if not form.is_valid():
            return _invalid(request, site, "member", form)
        site.users.add(form.member)
        transaction.on_commit(lambda: send_site_updated_message(site))
        transaction.on_commit(lambda: notify_site_member_added(site, form.member, request.user))
    messages.success(request, "Member added.")
    return _redirect(request, _settings_url(site))


@login_required
@require_POST
def remove_member(request, site_id, user_id):
    with transaction.atomic():
        site = _editable_site(request, site_id, lock=True)
        member = get_object_or_404(site.users, pk=user_id)
        if site.users.count() <= 1:
            messages.error(request, "At least one member must remain on the site.")
            return _redirect(request, _settings_url(site))
        site.users.remove(member)
        transaction.on_commit(lambda: send_site_updated_message(site))
    messages.success(request, "Member removed.")
    if member.pk == request.user.pk and not request.user.is_superuser:
        return _redirect(request, reverse("sites:index"))
    return _redirect(request, _settings_url(site))


@login_required
@require_POST
def add_domain(request, site_id):
    form = None
    site = None
    try:
        with transaction.atomic():
            site = _editable_site(request, site_id, lock=True)
            form = AddDomainForm(request.POST, site=site, user=request.user)
            if not form.is_valid():
                return _invalid(request, site, "domain", form)
            operation = _start_settings_operation(site, "edit_site_names")
            domain_name = form.cleaned_data["domain"]
            existing = (
                Domain.objects.select_for_update()
                .filter(site=site, domain__iexact=domain_name, status="inactive")
                .first()
            )
            if existing:
                existing.status = "active"
                existing.save(update_fields=["status"])
            else:
                Domain.objects.create(site=site, domain=domain_name, creating_user=request.user)
            transaction.on_commit(lambda: send_site_updated_message(site))
    except OperationInProgressError:
        form.add_error(None, "Finish or retry the current operation before changing domains.")
        return _invalid(request, site, "domain", form)
    except IntegrityError:
        form.add_error("domain", "This domain was assigned to another site. Choose another domain.")
        return _invalid(request, site, "domain", form)
    return _deployment_saved(request, site, operation)


@login_required
@require_POST
def remove_domain(request, site_id, domain_id):
    try:
        with transaction.atomic():
            site = _editable_site(request, site_id, lock=True)
            domain = get_object_or_404(
                Domain.objects.select_for_update(),
                site=site,
                pk=domain_id,
                status__in=["active", "inactive"],
            )
            operation = _start_settings_operation(site, "edit_site_names")
            domain.status = "removing"
            domain.save(update_fields=["status"])
            transaction.on_commit(lambda: send_site_updated_message(site))
    except OperationInProgressError:
        messages.error(request, "Finish or retry the current operation before changing domains.")
        return _redirect(request, _settings_url(site))
    return _deployment_saved(request, site, operation)
