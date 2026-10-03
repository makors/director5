"""Verify database ownership, queue failures, secret separation, and durable rotation."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from .. import database_actions, database_tasks
from ..models import Database, DatabaseHost, Operation, Site
from . import framework


@pytest.fixture(autouse=True)
def database_test_settings(settings):
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


@pytest.fixture
def database_host():
    return DatabaseHost.objects.create(
        dbms="postgres",
        hostname="db.localhost",
        port=5432,
        admin_hostname="internal-db.localhost",
        admin_port=5433,
        admin_username="admin",
        admin_password="private-admin-password",
    )


@pytest.fixture
def dynamic_site(student):
    site = Site.objects.create(name="database-site", mode="dynamic", purpose="project")
    site.users.add(student)
    return site


@pytest.fixture
def ready_database(dynamic_site, database_host):
    database = Database.objects.create(
        host=database_host, password="current-site-password", provisioned=True
    )
    dynamic_site.database = database
    dynamic_site.save(update_fields=["database"])
    return database


@pytest.mark.parametrize("endpoint", ("detail", "create", "delete", "rotate_password", "query"))
def test_other_users_cannot_access_site_database(client, teacher, dynamic_site, endpoint):
    client.force_login(teacher)
    method = client.post if endpoint in {"delete", "rotate_password"} else client.get
    assert method(reverse(f"databases:{endpoint}", args=[dynamic_site.id])).status_code == 404


def test_static_site_cannot_create_database(client, student, dynamic_site, database_host):
    dynamic_site.mode = "static"
    dynamic_site.save()
    client.force_login(student)
    with patch.object(database_tasks.create_database, "delay") as delay:
        response = client.post(
            reverse("databases:create", args=[dynamic_site.id]), {"host": database_host.id}
        )
    assert response.status_code == 302
    assert not Database.objects.exists()
    delay.assert_not_called()


def test_create_allocates_pending_database_and_queues_setup(
    client, student, dynamic_site, database_host
):
    client.force_login(student)
    with patch.object(database_tasks.create_database, "delay") as delay:
        response = client.post(
            reverse("databases:create", args=[dynamic_site.id]), {"host": database_host.id}
        )
    dynamic_site.refresh_from_db()
    assert response.url == reverse("sites:dashboard", args=[dynamic_site.id])
    assert dynamic_site.database.host_id == database_host.id
    assert dynamic_site.database.provisioned is False
    assert len(dynamic_site.database.password) >= 32
    delay.assert_called_once_with(dynamic_site.operation.id)


def test_failed_create_queue_preserves_retryable_pending_database(
    client, student, dynamic_site, database_host
):
    client.force_login(student)
    with patch.object(
        database_tasks.create_database, "delay", side_effect=RuntimeError("queue unavailable")
    ):
        response = client.post(
            reverse("databases:create", args=[dynamic_site.id]), {"host": database_host.id}
        )
    dynamic_site.refresh_from_db()
    assert response.url == reverse("databases:detail", args=[dynamic_site.id])
    assert dynamic_site.database.provisioned is False
    assert dynamic_site.operation.status == "failed"


def test_busy_site_creates_no_database(client, student, dynamic_site, database_host):
    dynamic_site.start_operation("restart_site")
    client.force_login(student)
    with patch.object(database_tasks.create_database, "delay") as delay:
        response = client.post(
            reverse("databases:create", args=[dynamic_site.id]), {"host": database_host.id}
        )
    assert response.status_code == 200
    assert response.context["form"].non_field_errors()
    assert not Database.objects.exists()
    delay.assert_not_called()


def test_unconfirmed_backend_success_does_not_mark_database_ready(dynamic_site, ready_database):
    ready_database.provisioned = False
    ready_database.save()
    operation = dynamic_site.start_operation("create_site_database")
    with framework.mock({"path": "/api/database/create", "data": {"ok": False}}):
        database_tasks.create_database(operation.id)
    ready_database.refresh_from_db()
    assert ready_database.provisioned is False
    assert operation.action_set.first().result is False


def test_credentials_are_owner_only_uncached_and_never_admin_credentials(
    client, student, dynamic_site, ready_database
):
    client.force_login(student)
    response = client.get(reverse("databases:detail", args=[dynamic_site.id]))
    assert response["Cache-Control"] == "no-store"
    assert b"current-site-password" in response.content
    assert b"private-admin-password" not in response.content
    assert b"internal-db.localhost" not in response.content
    assert b"DIRECTOR_DATABASE_URL" in response.content


def test_rotation_keeps_current_password_until_remote_success(
    client, student, dynamic_site, ready_database
):
    client.force_login(student)
    with patch.object(database_tasks.rotate_database_password, "delay") as delay:
        response = client.post(reverse("databases:rotate_password", args=[dynamic_site.id]))
    ready_database.refresh_from_db()
    assert response.url == reverse("sites:dashboard", args=[dynamic_site.id])
    assert ready_database.password == "current-site-password"
    assert len(ready_database.pending_password) >= 32
    delay.assert_called_once_with(Operation.objects.get().id)


def test_rotation_failure_preserves_old_and_pending_password(dynamic_site, ready_database):
    ready_database.pending_password = "long-new-site-password"
    ready_database.save()
    operation = dynamic_site.start_operation("regen_site_secrets")
    with framework.mock(
        {
            "path": "/api/database/rotate-password",
            "status_code": 502,
            "data": {
                "detail": {
                    "user_error": True,
                    "description": "Database unavailable",
                    "explanation": "Try again.",
                }
            },
        }
    ):
        database_tasks.rotate_database_password(operation.id)
    ready_database.refresh_from_db()
    assert ready_database.password == "current-site-password"
    assert ready_database.pending_password == "long-new-site-password"
    assert operation.action_set.first().result is False


def test_rotation_success_updates_password_then_service(dynamic_site, ready_database):
    ready_database.pending_password = "long-new-site-password"
    ready_database.save()
    operation = dynamic_site.start_operation("regen_site_secrets")
    with framework.mock(
        {"path": "/api/database/rotate-password", "data": {"ok": True}},
        {"path": "/api/docker/service/update", "data": {"ok": True}},
    ):
        database_tasks.rotate_database_password(operation.id)
    ready_database.refresh_from_db()
    assert ready_database.password == "long-new-site-password"
    assert ready_database.pending_password == ""
    assert not Operation.objects.exists()


def test_delete_requires_exact_site_name(client, student, dynamic_site, ready_database):
    client.force_login(student)
    with patch.object(database_tasks.delete_database, "delay") as delay:
        response = client.post(
            reverse("databases:delete", args=[dynamic_site.id]), {"confirmation": "wrong"}
        )
    assert response.url == reverse("databases:detail", args=[dynamic_site.id])
    delay.assert_not_called()
    assert Database.objects.exists()


def test_delete_failure_keeps_database_record(dynamic_site, ready_database):
    operation = dynamic_site.start_operation("delete_site_database")
    with framework.mock(
        {
            "path": "/api/database/delete",
            "status_code": 502,
            "data": {
                "detail": {
                    "user_error": True,
                    "description": "Delete failed",
                    "explanation": "Permission denied.",
                }
            },
        }
    ):
        database_tasks.delete_database(operation.id)
    dynamic_site.refresh_from_db()
    assert dynamic_site.database_id == ready_database.id
    assert operation.action_set.first().result is False


def test_delete_success_removes_record_and_service_database_env(dynamic_site, ready_database):
    operation = dynamic_site.start_operation("delete_site_database")
    with framework.mock(
        {"path": "/api/database/delete", "data": {"ok": True}},
        {"path": "/api/docker/service/update", "data": {"ok": True}},
    ):
        database_tasks.delete_database(operation.id)
    dynamic_site.refresh_from_db()
    assert dynamic_site.database is None
    assert not Database.objects.exists()
    assert "db" not in dynamic_site.serialize_for_appserver()
    assert not Operation.objects.exists()


def test_query_uses_site_credentials_and_escapes_results(
    client, student, dynamic_site, ready_database
):
    client.force_login(student)
    result = {
        "columns": ["value"],
        "rows": [["<script>alert(1)</script>"]],
        "affected_rows": 1,
        "truncated": False,
    }
    with framework.mock({"path": "/api/database/query", "data": result}):
        response = client.post(
            reverse("databases:query", args=[dynamic_site.id]), {"sql": "SELECT value FROM entries"}
        )
    assert response.status_code == 200
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.content
    assert b"private-admin-password" not in response.content
    payload = database_actions.database_query_payload(ready_database, "SELECT 1")
    assert payload["database"]["username"] == ready_database.username
    assert "admin" not in payload
    assert payload["connection"]["hostname"] == "internal-db.localhost"


def test_query_error_preserves_sql_and_returns_helpful_message(
    client, student, dynamic_site, ready_database
):
    client.force_login(student)
    with framework.mock(
        {
            "path": "/api/database/query",
            "status_code": 400,
            "data": {
                "detail": {
                    "user_error": True,
                    "description": "Query failed",
                    "explanation": "Table missing.",
                }
            },
        }
    ):
        response = client.post(
            reverse("databases:query", args=[dynamic_site.id]), {"sql": "SELECT missing_table"}
        )
    assert response.context["form"]["sql"].value() == "SELECT missing_table"
    assert "Table missing" in str(response.context["form"].non_field_errors())


def test_query_without_confirmed_result_shows_no_success(
    client, student, dynamic_site, ready_database
):
    client.force_login(student)
    with framework.mock({"path": "/api/database/query", "data": {"ok": False}}):
        response = client.post(
            reverse("databases:query", args=[dynamic_site.id]), {"sql": "SELECT 1"}
        )
    assert response.context["result"] is None
    assert response.context["form"]["sql"].value() == "SELECT 1"
    assert response.context["form"].non_field_errors()


def test_admin_secrets_are_only_in_management_payload(dynamic_site, ready_database):
    management = database_actions.database_management_payload(dynamic_site)
    assert management["admin"]["password"] == "private-admin-password"
    regular = dynamic_site.serialize_for_appserver()
    assert "private-admin-password" not in str(regular)
    options = database_actions.database_runtime_options(ready_database)
    assert "current-site-password" not in " ".join(options["command"])
    assert options["environment"] == {"PGPASSWORD": "current-site-password"}
