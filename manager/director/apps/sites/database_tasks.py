"""Retryable database operations followed by service environment updates."""

from celery import shared_task

from . import actions, database_actions
from .operations import auto_run_operation_wrapper


@shared_task
def create_database(operation_id: int) -> None:
    with auto_run_operation_wrapper(operation_id) as wrapper:
        wrapper.register_action("Creating database", database_actions.create_site_database)
        wrapper.register_action("Updating application connection", actions.update_docker_service)


@shared_task
def delete_database(operation_id: int) -> None:
    with auto_run_operation_wrapper(operation_id) as wrapper:
        wrapper.register_action("Deleting database", database_actions.delete_site_database)
        wrapper.register_action(
            "Removing database connection", database_actions.forget_site_database
        )
        wrapper.register_action("Updating application connection", actions.update_docker_service)


@shared_task
def rotate_database_password(operation_id: int) -> None:
    with auto_run_operation_wrapper(operation_id) as wrapper:
        wrapper.register_action(
            "Changing database password", database_actions.rotate_database_password
        )
        wrapper.register_action("Updating application connection", actions.update_docker_service)


RETRY_TASKS = {
    "create_site_database": create_database,
    "delete_site_database": delete_database,
    "regen_site_secrets": rotate_database_password,
}
