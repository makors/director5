from functools import wraps

from celery import shared_task
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.views.decorators.http import require_POST

from . import actions
from .models import Action, Operation, OperationInProgressError, Site
from .operations import auto_run_operation_wrapper
from .views import _perform_operation, _queue_operation, retry_operation


def administrator(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden("Administrator access required.")
        return view(request, *args, **kwargs)

    return wrapped


class AvailabilityForm(forms.ModelForm):
    confirmation = forms.CharField(
        label="Confirm the site name",
        strip=False,
        widget=forms.TextInput(attrs={"class": "dt-input block"}),
    )

    class Meta:
        model = Site
        fields = ["availability"]
        widgets = {"availability": forms.Select(attrs={"class": "dt-input block"})}

    def clean_confirmation(self):
        value = self.cleaned_data["confirmation"]
        if value != self.instance.name:
            raise forms.ValidationError("The site name must match exactly.")
        return value


@shared_task
def apply_availability(operation_id):
    with auto_run_operation_wrapper(operation_id) as wrapper:
        wrapper.register_action("Updating public access", actions.update_docker_service)


@administrator
def management(request):
    operations = (
        Operation.objects.select_related("site")
        .prefetch_related("action_set")
        .order_by("-created_time")
    )
    if request.GET.get("failed"):
        operations = operations.filter(action__result=False).distinct()
    page = Paginator(operations, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "sites/management/index.html",
        {"page_obj": page, "failed_only": bool(request.GET.get("failed"))},
    )


@administrator
def availability(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    form = AvailabilityForm(request.POST or None, instance=site)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                site = Site.objects.select_for_update().get(pk=site_id)
                operation = site.start_operation("update_availability")
                site.availability = form.cleaned_data["availability"]
                site.save(update_fields=["availability"])
        except OperationInProgressError:
            form.add_error(None, "Finish or retry the existing operation first.")
        else:
            _queue_operation(site, operation, apply_availability)
            return redirect("sites:dashboard", site.pk)
    return render(
        request,
        "sites/management/availability.html",
        {"form": form, "site": site, "can_edit": True},
    )


@administrator
@require_POST
def reset_failed_operation(request, operation_id):
    operation = get_object_or_404(Operation.objects.select_related("site"), pk=operation_id)
    if request.POST.get("confirmation") != operation.site.name:
        messages.error(request, "Enter the site name to confirm retrying this operation.")
        return redirect("sites:management")
    if operation.status != "failed":
        messages.error(
            request,
            "Running or queued work cannot be reset safely. Only failed operations can be retried.",
        )
        return redirect("sites:management")
    return retry_operation(request, operation.site_id, expected_operation_id=operation.pk)


@login_required
@require_POST
def reapply_configuration(request, site_id):
    from .settings_tasks import apply_site_settings

    site = get_object_or_404(Site.objects.filter_editable(request.user), pk=site_id)
    return _perform_operation(request, site, "regen_nginx_config", apply_site_settings)


def metrics(request):
    address = request.META.get("REMOTE_ADDR", "")
    is_admin = request.user.is_authenticated and request.user.is_superuser
    if not is_admin and address not in getattr(settings, "DIRECTOR_METRICS_SCRAPE_IPS", []):
        return HttpResponseForbidden("Metrics access required.")
    count = Action.objects.filter(result=False, user_recoverable=False).count()
    return HttpResponse(
        f"# TYPE director_sites_failed_actions gauge\ndirector_sites_failed_actions {count}\n",
        content_type="text/plain; version=0.0.4",
    )


urlpatterns = [
    path("management/", management, name="management"),
    path(
        "management/operations/<int:operation_id>/retry/",
        reset_failed_operation,
        name="operation-reset",
    ),
    path("management/sites/<int:site_id>/availability/", availability, name="availability"),
    path(
        "configuration/<int:site_id>/reapply/", reapply_configuration, name="configuration-reapply"
    ),
    path("prometheus-metrics", metrics, name="metrics"),
]
