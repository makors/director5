"""Deployment tasks for site settings and custom domain changes."""

from collections.abc import Iterator

from celery import shared_task
from django.db import transaction

from . import actions
from .appserver import Appserver
from .models import Domain, Site
from .operations import auto_run_operation_wrapper


def release_removed_domains(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    """Release hostname ownership only after its old route has been removed."""
    with transaction.atomic():
        Site.objects.select_for_update().get(pk=site.pk)
        domains = Domain.objects.filter(site=site, status="removing")
        count = domains.update(site=None, status="deleted")
    yield f"Released {count} removed domain(s)."


@shared_task
def apply_site_settings(operation_id: int) -> None:
    with auto_run_operation_wrapper(operation_id) as wrapper:
        if wrapper.operation.ty == "change_site_type":
            wrapper.register_action(
                "Rebuilding Docker image", actions.build_docker_image, user_recoverable=True
            )
        wrapper.register_action("Updating site routing", actions.update_docker_service)
        if wrapper.site.domain_set.filter(status="removing").exists():
            wrapper.register_action("Releasing removed domains", release_removed_domains)
