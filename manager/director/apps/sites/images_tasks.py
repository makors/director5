"""Image and resource changes use the same observable hosting operations."""

from celery import shared_task

from . import actions
from .operations import auto_run_operation_wrapper


def finish_image_configuration(site, _appservers):
    site.image_write_run_script = False
    site.save(update_fields=["image_write_run_script"])
    yield "Image configuration saved"


@shared_task
def rebuild_image(operation_id: int) -> None:
    with auto_run_operation_wrapper(operation_id) as wrapper:
        wrapper.register_action(
            "Building selected image", actions.build_docker_image, user_recoverable=True
        )
        wrapper.register_action("Deploying selected image", actions.update_docker_service)
        wrapper.register_action("Finishing image setup", finish_image_configuration)


@shared_task
def apply_resource_limits(operation_id: int) -> None:
    with auto_run_operation_wrapper(operation_id) as wrapper:
        wrapper.register_action("Applying resource limits", actions.update_docker_service)
