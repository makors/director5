import traceback

from celery import shared_task
from django.conf import settings
from django.db import transaction

from . import actions
from .models import Action, Site
from .operations import (
    auto_run_operation_wrapper,
    send_operation_updated_message,
    send_site_deleted_message,
)


@shared_task
def create_site(operation_id: int) -> None:
    with auto_run_operation_wrapper(operation_id) as wrapper:
        wrapper.register_action(
            "Building Docker image",
            actions.build_docker_image,
            user_recoverable=True,
        )
        wrapper.register_action("Creating Docker service", actions.update_docker_service)


@shared_task
def restart_site_process(operation_id: int) -> None:
    with auto_run_operation_wrapper(operation_id) as wrapper:
        wrapper.register_action("Restarting Docker service", actions.update_docker_service)


@shared_task
def rebuild_docker_image(operation_id: int) -> None:
    with auto_run_operation_wrapper(operation_id) as wrapper:
        wrapper.register_action(
            "Rebuilding Docker image",
            actions.build_docker_image,
            user_recoverable=True,
        )
        wrapper.register_action("Restarting Docker service", actions.update_docker_service)


@shared_task
def delete_site(operation_id: int) -> None:
    site = Site.objects.get(operation__id=operation_id)

    with auto_run_operation_wrapper(operation_id, clear_on_success=False) as wrapper:
        if settings.SITE_DELETION_REMOVE_FILES:
            wrapper.register_action("Deleting site files", actions.delete_site_files)
        if settings.SITE_DELETION_REMOVE_DATABASE and site.database_id:
            wrapper.register_action("Deleting site database", actions.delete_site_database)

        wrapper.register_action("Deleting Docker service", actions.remove_docker_service)
        wrapper.register_action("Deleting Docker image", actions.remove_docker_image)

    if wrapper.result:
        database = site.database
        site_id = site.id
        try:
            with transaction.atomic():
                # Keep the operation until local cleanup is locked and committed so another
                # request cannot start hosting work after remote resources have been removed.
                Site.objects.select_for_update().get(pk=site.pk)
                wrapper.operation.action_set.all().delete()
                wrapper.operation.delete()
                # Keep the domain audit record while releasing its protected site reference.
                site.domain_set.update(site=None, status="deleted")
                site.delete()
                if settings.SITE_DELETION_REMOVE_DATABASE and database:
                    database.delete()
        except Exception:  # noqa: BLE001
            # The rollback restores the site and operation; record a retryable local failure.
            site.id = site_id
            Action.objects.create(
                operation_id=operation_id,
                slug="finalize_deletion",
                name="Finishing site cleanup",
                result=False,
                message=traceback.format_exc(),
                user_message="Site cleanup could not finish. Please retry the deletion.",
            )
            send_operation_updated_message(site)
            return
        # Django clears the primary key on delete; the channel still uses the old ID.
        site.id = site_id
        send_site_deleted_message(site)
