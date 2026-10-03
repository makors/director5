"""Approved base images, reusable setup commands, and site resource controls."""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from . import images_tasks
from .images_forms import (
    DockerImageForm,
    DockerImageSetupCommandForm,
    ImageSelectForm,
    SiteResourceLimitsForm,
)
from .models import (
    DockerImage,
    DockerImageSetupCommand,
    Operation,
    OperationInProgressError,
    Site,
    SiteResourceLimits,
)
from .views import _queue_operation


def administrator_required(view):
    @login_required
    @wraps(view)
    def guarded(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return guarded


def form_context(form, **kwargs):
    form.prepare_errors()
    return {"form": form, **kwargs}


@administrator_required
def image_catalog(request):
    return render(
        request,
        "sites/images/catalog.html",
        {
            "images": DockerImage.objects.prefetch_related("setup_commands").order_by(
                "friendly_name", "name"
            ),
            "setup_commands": DockerImageSetupCommand.objects.order_by("order", "id"),
        },
    )


@administrator_required
def image_edit(request, image_id=None):
    image = get_object_or_404(DockerImage, pk=image_id) if image_id else None
    form = DockerImageForm(request.POST if request.method == "POST" else None, instance=image)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Image saved. Sites use these settings on their next rebuild.")
        return redirect("sites:image-catalog")
    return render(
        request,
        "sites/images/edit.html",
        form_context(
            form,
            title="Edit image" if image else "Add image",
            image=image,
            cancel_url=reverse("sites:image-catalog"),
            button_label="Save image",
        ),
    )


@administrator_required
def setup_edit(request, command_id=None):
    command = get_object_or_404(DockerImageSetupCommand, pk=command_id) if command_id else None
    form = DockerImageSetupCommandForm(
        request.POST if request.method == "POST" else None, instance=command
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Setup command saved. Sites use it on their next rebuild.")
        return redirect("sites:image-catalog")
    return render(
        request,
        "sites/images/edit.html",
        form_context(
            form,
            title="Edit setup command" if command else "Add setup command",
            command=command,
            cancel_url=reverse("sites:image-catalog"),
            button_label="Save command",
        ),
    )


@administrator_required
def image_delete(request, image_id):
    image = get_object_or_404(DockerImage, pk=image_id)
    used = Site.objects.filter(docker_image=image).exists()
    if request.method == "POST":
        if request.POST.get("confirmation") != image.name:
            messages.error(request, "Enter the image reference exactly to confirm deletion.")
        elif used:
            messages.error(
                request, "This image is used by a site. Choose another image for that site first."
            )
        else:
            try:
                image.delete()
            except ProtectedError:
                messages.error(request, "This image is now in use and cannot be deleted.")
            else:
                messages.success(request, "Image deleted.")
                return redirect("sites:image-catalog")
    return render(
        request,
        "sites/images/delete.html",
        {
            "title": "Delete image",
            "name": image.name,
            "used": used,
            "description": "Sites using this image must be changed before it can be deleted.",
        },
    )


@administrator_required
def setup_delete(request, command_id):
    command = get_object_or_404(DockerImageSetupCommand, pk=command_id)
    if request.method == "POST":
        if request.POST.get("confirmation") == command.name:
            command.delete()
            messages.success(
                request,
                "Setup command deleted. Existing running sites change only on their next rebuild.",
            )
            return redirect("sites:image-catalog")
        messages.error(request, "Enter the command name exactly to confirm deletion.")
    return render(
        request,
        "sites/images/delete.html",
        {
            "title": "Delete setup command",
            "name": command.name,
            "description": "This removes the command from every image that uses it. Existing sites use the change on their next rebuild.",
        },
    )


def save_hosting_change(request, site, kind, task, apply):
    """Persist desired configuration and reserve its operation in one transaction."""
    try:
        with transaction.atomic():
            locked = Site.objects.select_for_update().get(pk=site.pk)
            current = Operation.objects.filter(site=locked).first()
            replace_failed = can_replace_failed(current, kind)
            if current and not replace_failed:
                raise OperationInProgressError
            apply(locked)
            operation = locked.start_operation(kind, replace_failed=replace_failed)
    except OperationInProgressError:
        messages.error(
            request,
            "Another operation needs to finish or be retried before changing these settings.",
        )
        return False
    if _queue_operation(locked, operation, task):
        messages.success(
            request, "Changes saved and queued. Follow their deployment progress on Overview."
        )
    else:
        messages.error(
            request, "Changes were saved, but hosting could not start. Use Retry on Overview."
        )
    return True


def can_replace_failed(operation, kind):
    if not operation or operation.status != "failed":
        return False
    allowed = {kind}
    if kind == "update_docker_image":
        allowed.update({"create_site", "fix_site"})
    return operation.ty in allowed


def operation_context(site, kind):
    operation = Operation.objects.filter(site=site).first()
    return {
        "operation": operation,
        "can_change": not operation or can_replace_failed(operation, kind),
    }


@login_required
def image_select(request, site_id):
    site = get_object_or_404(Site.objects.filter_editable(request.user), pk=site_id)
    form = ImageSelectForm(
        request.POST if request.method == "POST" else None, site=site, user=request.user
    )
    if request.method == "POST" and form.is_valid():

        def apply(locked):
            locked.docker_image = form.cleaned_data["image"]
            locked.image_packages = form.cleaned_data["packages"]
            locked.image_write_run_script = form.cleaned_data["write_run_sh_file"]
            locked.save(update_fields=["docker_image", "image_packages", "image_write_run_script"])

        if save_hosting_change(
            request, site, "update_docker_image", images_tasks.rebuild_image, apply
        ):
            return redirect("sites:dashboard", site_id=site.pk)
    return render(
        request,
        "sites/images/select.html",
        form_context(
            form,
            site=site,
            images=form.fields["image"].queryset,
            cancel_url=reverse("sites:dashboard", args=[site.pk]),
            **operation_context(site, "update_docker_image"),
        ),
    )


@administrator_required
def resource_list(request):
    limits = (
        SiteResourceLimits.objects.filter(
            Q(cpus__isnull=False) | ~Q(memory="") | Q(max_request_body_size__isnull=False)
        )
        .select_related("site")
        .order_by("site__name")
    )
    return render(request, "sites/images/resources.html", {"limits": limits})


@administrator_required
def resource_edit(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    limits = SiteResourceLimits.objects.filter(site=site).first()
    form = SiteResourceLimitsForm(
        request.POST if request.method == "POST" else None, instance=limits
    )
    if request.method == "POST" and form.is_valid():

        def apply(locked):
            changes = form.save(commit=False)
            changes.site = locked
            changes.save()

        if save_hosting_change(
            request, site, "update_resource_limits", images_tasks.apply_resource_limits, apply
        ):
            return redirect("sites:dashboard", site_id=site.pk)
    return render(
        request,
        "sites/images/resource_edit.html",
        form_context(
            form,
            site=site,
            cancel_url=reverse("sites:dashboard", args=[site.pk]),
            **operation_context(site, "update_resource_limits"),
        ),
    )
