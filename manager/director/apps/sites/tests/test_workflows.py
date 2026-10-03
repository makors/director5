"""Verify site permissions, queueing, progress, and safe deletion."""

from unittest.mock import Mock, patch

import pytest
from django.urls import reverse

from .. import tasks
from ..models import (
    Action,
    Database,
    DatabaseHost,
    Domain,
    Operation,
    OperationInProgressError,
    Site,
)
from ..operations import OperationWrapper, auto_run_operation_wrapper
from . import framework


@pytest.fixture(autouse=True)
def memory_channels(settings):
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


@pytest.fixture
def site(student):
    result = Site.objects.create(name="test-site", mode="static", purpose="project")
    result.users.add(student)
    return result


@pytest.mark.parametrize("mode", ("static", "dynamic"))
@pytest.mark.parametrize("htmx", (False, True))
def test_create_redirects_to_dashboard(client, student, mode, htmx):
    client.force_login(student)
    with patch.object(tasks.create_site, "delay") as delay:
        response = client.post(
            reverse("sites:create"),
            {
                "name": "new-site",
                "description": "A useful site",
                "purpose": "project",
                "mode": mode,
                "student_agreement": True,
            },
            headers={"HX-Request": "true"} if htmx else {},
        )
    created = Site.objects.get(name="new-site")
    assert created.users.filter(pk=student.pk).exists()
    assert created.operation.ty == "create_site"
    delay.assert_called_once_with(created.operation.id)
    dashboard = reverse("sites:dashboard", args=[created.id])
    if htmx:
        assert response.status_code == 200
        assert response["HX-Redirect"] == dashboard
    else:
        assert response.status_code == 302
        assert response.url == dashboard


def test_queue_failure_is_retryable(client, student):
    client.force_login(student)
    with patch.object(tasks.create_site, "delay", side_effect=RuntimeError("queue offline")):
        response = client.post(
            reverse("sites:create"),
            {
                "name": "new-site",
                "purpose": "project",
                "mode": "static",
                "student_agreement": True,
            },
        )
    assert response.status_code == 302
    operation = Operation.objects.get(site__name="new-site")
    assert operation.status == "failed"
    assert operation.action_set.get().user_message
    with patch.object(tasks.create_site, "delay") as delay:
        response = client.post(
            reverse("sites:retry", args=[operation.site_id]), {"tab": "settings"}
        )
    assert response.url == reverse("sites:dashboard", args=[operation.site_id])
    replacement = Operation.objects.get(site__name="new-site")
    assert replacement.id != operation.id
    assert replacement.status == "queued"
    delay.assert_called_once_with(replacement.id)


def test_invalid_creation_preserves_values_and_queues_nothing(client, student):
    client.force_login(student)
    with patch.object(tasks.create_site, "delay") as delay:
        response = client.post(
            reverse("sites:create"),
            {
                "name": "UPPERCASE",
                "description": "Keep this",
                "purpose": "project",
                "mode": "dynamic",
                "student_agreement": True,
            },
        )
    assert response.status_code == 200
    assert response.context["form"].errors["name"]
    assert response.context["form"]["description"].value() == "Keep this"
    assert response.context["form"]["mode"].value() == "dynamic"
    assert not Site.objects.exists()
    delay.assert_not_called()


def test_search_and_type_filter_keep_private_sites_hidden(client, student, teacher, site):
    Site.objects.create(name="private-site", mode="dynamic", purpose="project").users.add(teacher)
    site.description = "Searchable phrase"
    site.save()
    client.force_login(student)
    response = client.get(reverse("sites:index"), {"q": "Searchable", "mode": "static"})
    assert list(response.context["sites"]) == [site]
    assert response.context["total_sites"] == 1
    assert response.context["q"] == "Searchable"
    response = client.get(reverse("sites:index"), {"mode": "dynamic"})
    assert list(response.context["sites"]) == []


@pytest.mark.parametrize("action", ("dashboard", "delete", "restart", "rebuild", "retry"))
def test_other_users_cannot_access_site(client, teacher, site, action):
    client.force_login(teacher)
    endpoint = reverse(f"sites:{action}", args=[site.id])
    response = client.get(endpoint) if action == "dashboard" else client.post(endpoint)
    assert response.status_code == 404
    assert not Operation.objects.exists()


@pytest.mark.parametrize("action", ("delete", "restart", "rebuild", "retry"))
def test_site_mutations_require_post(client, student, site, action):
    client.force_login(student)
    assert client.get(reverse(f"sites:{action}", args=[site.id])).status_code == 405
    assert not Operation.objects.exists()


def test_disabled_site_cannot_be_mutated_by_owner(client, student, site):
    site.availability = "disabled"
    site.save()
    client.force_login(student)
    assert client.get(reverse("sites:dashboard", args=[site.id])).status_code == 200
    assert client.post(reverse("sites:restart", args=[site.id])).status_code == 404


def test_delete_requires_exact_confirmation(client, student, site):
    client.force_login(student)
    with patch.object(tasks.delete_site, "delay") as delay:
        response = client.post(reverse("sites:delete", args=[site.id]), {"confirmation": "wrong"})
    assert response.status_code == 302
    assert not Operation.objects.exists()
    delay.assert_not_called()
    with patch.object(tasks.delete_site, "delay") as delay:
        response = client.post(reverse("sites:delete", args=[site.id]), {"confirmation": site.name})
    assert response.status_code == 302
    delay.assert_called_once_with(site.operation.id)


def test_second_operation_is_rejected_and_failure_can_be_replaced(site):
    operation = site.start_operation("create_site")
    with pytest.raises(OperationInProgressError):
        site.start_operation("restart_site")
    Action.objects.create(
        operation=operation, slug="build_image", name="Building image", result=False
    )
    replacement = site.start_operation("delete_site", replace_failed=True)
    assert replacement.ty == "delete_site"
    assert not Action.objects.exists()
    assert Operation.objects.count() == 1


def test_restart_during_active_operation_returns_message(client, student, site):
    site.start_operation("create_site")
    client.force_login(student)
    with patch.object(tasks.restart_site_process, "delay") as delay:
        response = client.post(reverse("sites:restart", args=[site.id]))
    assert response.status_code == 302
    assert Operation.objects.count() == 1
    delay.assert_not_called()


def test_missing_appserver_marks_action_failed_instead_of_stuck(site):
    operation = site.start_operation("create_site")
    wrapper = OperationWrapper(operation)
    callback = Mock(__name__="build_image")
    wrapper.register_action("Building image", callback)
    with patch(
        "director.apps.sites.appserver.Appserver.list_pingable", side_effect=RuntimeError("Offline")
    ):
        assert wrapper.execute_operation() is False
    operation.refresh_from_db()
    assert operation.started_time is not None
    assert operation.status == "failed"
    action = operation.action_set.get()
    assert action.result is False
    assert "retry" in action.user_message
    callback.assert_not_called()


def test_delete_failure_retains_site_and_operation(site):
    operation = site.start_operation("delete_site")
    with framework.mock(
        {"path": "/api/files/delete-all", "data": {"error": "offline"}, "status_code": 500}
    ):
        tasks.delete_site(operation.id)
    assert Site.objects.filter(pk=site.pk).exists()
    operation.refresh_from_db()
    assert operation.status == "failed"
    assert operation.action_set.first().result is False


def test_nested_orchestrator_user_error_is_displayed(site):
    operation = site.start_operation("create_site")
    with framework.mock(
        {
            "path": "/api/docker/image/build",
            "status_code": 500,
            "data": {
                "detail": {
                    "description": "Build failed",
                    "explanation": "Fix the invalid Dockerfile.",
                    "user_error": True,
                }
            },
        }
    ):
        tasks.create_site(operation.id)
    action = operation.action_set.first()
    assert action.result is False
    assert action.user_message == "Build failed: Fix the invalid Dockerfile."


def test_delete_operation_retains_lock_after_remote_cleanup(site):
    operation = site.start_operation("delete_site")

    def remove_resources(_site, _appservers):
        yield "Resources removed"

    with (
        patch("director.apps.sites.appserver.Appserver.list_pingable", return_value=[]),
        auto_run_operation_wrapper(operation.id, clear_on_success=False) as wrapper,
    ):
        wrapper.register_action("Removing resources", remove_resources)

    assert wrapper.result is True
    assert Operation.objects.filter(pk=operation.id).exists()
    with pytest.raises(OperationInProgressError):
        site.start_operation("restart_site")


def test_delete_success_releases_domains_and_removes_database(site):
    host = DatabaseHost.objects.create(
        dbms="postgres",
        hostname="db.localhost",
        port=5432,
        admin_username="admin",
        admin_password="test",
    )
    database = Database.objects.create(host=host, password="test")
    site.database = database
    site.save()
    domain = Domain.objects.create(site=site, domain="example.org")
    operation = site.start_operation("delete_site")
    endpoints = (
        "/api/files/delete-all",
        "/api/database/delete",
        "/api/docker/service/remove",
        "/api/docker/image/delete",
    )
    with framework.mock(*({"path": endpoint, "data": {"ok": True}} for endpoint in endpoints)):
        tasks.delete_site(operation.id)
    assert not Site.objects.filter(name="test-site").exists()
    assert not Operation.objects.exists()
    assert not Database.objects.filter(pk=database.pk).exists()
    domain.refresh_from_db()
    assert domain.site_id is None
    assert domain.status == "deleted"


def test_final_cleanup_failure_rolls_back_metadata_and_remains_retryable(site):
    domain = Domain.objects.create(site=site, domain="example.org")
    operation = site.start_operation("delete_site")
    endpoints = ("/api/files/delete-all", "/api/docker/service/remove", "/api/docker/image/delete")
    with (
        framework.mock(*({"path": endpoint, "data": {"ok": True}} for endpoint in endpoints)),
        patch("django.db.models.query.QuerySet.update", side_effect=RuntimeError("Cleanup failed")),
    ):
        tasks.delete_site(operation.id)
    assert Site.objects.filter(name="test-site").exists()
    operation.refresh_from_db()
    assert operation.status == "failed"
    assert operation.action_set.get(slug="finalize_deletion").user_message
    domain.refresh_from_db()
    assert domain.site_id == site.id
    assert domain.status == "active"


def test_public_urls_and_routing_hosts_are_serialized_separately(site, settings):
    settings.SITE_URL_FORMATS = {"user": "users.localhost/{}/", None: "{}.sites.example.org"}
    Domain.objects.create(site=site, domain="custom.example.org")
    Domain.objects.create(site=site, domain="inactive.example.org", status="inactive")
    site.purpose = "user"
    assert site.sites_url == "http://users.localhost/test-site/"
    data = site.serialize_for_appserver()
    assert data["hosts"] == ["custom.example.org", "users.localhost"]
    assert data["routes"] == [
        {"host": "custom.example.org", "path_prefix": "/"},
        {"host": "users.localhost", "path_prefix": "/test-site/"},
    ]


def test_database_identity_matches_url_and_survives_site_rename(site):
    host = DatabaseHost.objects.create(
        dbms="postgres",
        hostname="db.localhost",
        port=5432,
        admin_username="admin",
        admin_password="test",
    )
    database = Database.objects.create(host=host, password="test-password")
    site.database = database
    site.save()
    database_name = f"site_{site.id}"
    serialized = database.serialize_for_appserver()
    assert serialized["name"] == serialized["username"] == database_name
    assert serialized["url"].endswith(f"/{database_name}")
    site.name = "renamed-site"
    site.save()
    database.refresh_from_db()
    assert database.serialize_for_appserver() == serialized


@pytest.mark.parametrize(
    ("requested", "expected"),
    (
        ("overview", "overview"),
        ("settings", "settings"),
        ("unknown", "overview"),
        ("https://example.org", "overview"),
    ),
)
def test_dashboard_tabs_are_allowlisted(client, student, site, requested, expected):
    client.force_login(student)
    response = client.get(reverse("sites:dashboard", args=[site.id]), {"tab": requested})
    assert response.context["dashboard_tab"] == expected


@pytest.mark.parametrize(
    ("action", "task_name"),
    (("restart", "restart_site_process"), ("rebuild", "rebuild_docker_image")),
)
def test_successful_site_action_returns_overview(client, student, site, action, task_name):
    client.force_login(student)
    with patch.object(getattr(tasks, task_name), "delay"):
        response = client.post(reverse(f"sites:{action}", args=[site.id]), {"tab": "settings"})
    assert response.url == reverse("sites:dashboard", args=[site.id])


@pytest.mark.parametrize("htmx", (False, True))
def test_site_queue_failure_retains_settings_tab(client, student, site, htmx):
    client.force_login(student)
    with patch.object(
        tasks.restart_site_process, "delay", side_effect=RuntimeError("queue offline")
    ):
        response = client.post(
            reverse("sites:restart", args=[site.id]),
            {"tab": "settings"},
            headers={"HX-Request": "true"} if htmx else {},
        )
    target = reverse("sites:dashboard", args=[site.id]) + "?tab=settings"
    if htmx:
        assert response["HX-Redirect"] == target
    else:
        assert response.url == target


@pytest.mark.parametrize("action", ("restart", "rebuild", "retry", "delete"))
def test_site_action_errors_retain_settings_tab(client, student, site, action):
    client.force_login(student)
    if action != "retry":
        site.start_operation("create_site")
    data = {"tab": "settings", "confirmation": site.name}
    response = client.post(reverse(f"sites:{action}", args=[site.id]), data)
    assert response.url == reverse("sites:dashboard", args=[site.id]) + "?tab=settings"


def test_delete_validation_error_retains_settings_tab(client, student, site):
    client.force_login(student)
    response = client.post(
        reverse("sites:delete", args=[site.id]),
        {
            "tab": "settings",
            "confirmation": "wrong",
        },
    )
    assert response.url == reverse("sites:dashboard", args=[site.id]) + "?tab=settings"


def test_successful_deletion_from_settings_returns_site_list(client, student, site):
    client.force_login(student)
    with patch.object(tasks.delete_site, "delay"):
        response = client.post(
            reverse("sites:delete", args=[site.id]),
            {
                "tab": "settings",
                "confirmation": site.name,
            },
        )
    assert response.url == reverse("sites:index")
