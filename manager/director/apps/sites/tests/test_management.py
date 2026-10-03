"""Verify administrator controls, recovery dispatch, and metrics access."""

import json
from unittest.mock import patch

import pytest
import responses
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from .. import management_views, settings_tasks, tasks
from ..appserver import Appserver
from ..models import Action, Operation, Site
from . import framework


@pytest.fixture(autouse=True)
def memory_channels(settings):
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


@pytest.fixture
def admin(django_user_model):
    return django_user_model.objects.create_user(
        username="management-admin", is_superuser=True, is_staff=True, accepted_guidelines=True
    )


@pytest.fixture
def site(student):
    result = Site.objects.create(name="test-site", mode="static", purpose="project")
    result.users.add(student)
    return result


def failed_operation(site, kind="update_availability"):
    operation = site.start_operation(kind)
    Action.objects.create(
        operation=operation,
        slug="failed_step",
        name="Failed step",
        result=False,
        message="Internal diagnostic",
    )
    return operation


def availability_url(site):
    return reverse("sites:availability", args=[site.id])


@pytest.mark.parametrize("action", ("management", "availability", "operation-reset"))
def test_anonymous_cannot_use_management(client, site, action):
    operation = failed_operation(site)
    args = [] if action == "management" else [site.pk if action == "availability" else operation.pk]
    endpoint = reverse("sites:" + action, args=args)
    response = client.post(endpoint) if action == "operation-reset" else client.get(endpoint)
    assert response.status_code == 302
    assert response.url.startswith(reverse("auth:login"))
    assert Operation.objects.get(site=site).pk == operation.pk


@pytest.mark.parametrize("staff", (False, True))
@pytest.mark.parametrize("action", ("management", "availability", "operation-reset"))
def test_members_and_staff_need_superuser_for_management(client, student, site, staff, action):
    student.is_staff = staff
    student.save(update_fields=["is_staff"])
    operation = failed_operation(site)
    client.force_login(student)
    args = [] if action == "management" else [site.pk if action == "availability" else operation.pk]
    endpoint = reverse("sites:" + action, args=args)
    response = client.post(endpoint) if action == "operation-reset" else client.get(endpoint)
    assert response.status_code == 403
    assert Operation.objects.get(site=site).pk == operation.pk
    assert site.availability == "enabled"


def test_superuser_sees_operations_for_nonmember_sites(client, admin, site):
    operation = failed_operation(site)
    client.force_login(admin)
    response = client.get(reverse("sites:management"))
    assert response.status_code == 200
    assert list(response.context["page_obj"]) == [operation]
    assert b"Internal diagnostic" in response.content
    assert client.get(availability_url(site)).status_code == 200


def test_management_failed_filter_excludes_queued_and_running_and_deduplicates(client, admin, site):
    failed = failed_operation(site)
    Action.objects.create(
        operation=failed, slug="another_failure", name="Another failure", result=False
    )
    for name, started_time in (("queued-site", None), ("running-site", timezone.now())):
        other = Site.objects.create(name=name, mode="static", purpose="project")
        Operation.objects.create(site=other, ty="restart_site", started_time=started_time)
    client.force_login(admin)
    response = client.get(reverse("sites:management"), {"failed": "1"})
    assert list(response.context["page_obj"]) == [failed]
    assert response.context["failed_only"] is True


def test_management_is_paginated(client, admin):
    for index in range(31):
        other = Site.objects.create(name=f"site-{index}", mode="static", purpose="project")
        Operation.objects.create(site=other, ty="restart_site")
    client.force_login(admin)
    response = client.get(reverse("sites:management"))
    assert len(response.context["page_obj"]) == 30
    assert response.context["page_obj"].has_next()
    response = client.get(reverse("sites:management"), {"page": "2"})
    assert len(response.context["page_obj"]) == 1


@pytest.mark.parametrize("value", ("", "wrong", "TEST-SITE", " test-site", "test-site "))
def test_availability_requires_exact_name_confirmation(client, admin, site, value):
    client.force_login(admin)
    with patch.object(management_views.apply_availability, "delay") as delay:
        response = client.post(
            availability_url(site), {"availability": "disabled", "confirmation": value}
        )
    assert response.status_code == 200
    assert response.context["form"].errors["confirmation"]
    site.refresh_from_db()
    assert site.availability == "enabled"
    assert not Operation.objects.exists()
    delay.assert_not_called()


def test_availability_rejects_unknown_value_preserving_form(client, admin, site):
    client.force_login(admin)
    response = client.post(
        availability_url(site), {"availability": "invalid", "confirmation": site.name}
    )
    assert response.status_code == 200
    assert response.context["form"]["availability"].value() == "invalid"
    assert response.context["form"].errors["availability"]
    site.refresh_from_db()
    assert site.availability == "enabled"
    assert not Operation.objects.exists()


@pytest.mark.parametrize(
    ("initial", "target"),
    (
        ("enabled", "not-served"),
        ("enabled", "disabled"),
        ("disabled", "enabled"),
        ("not-served", "enabled"),
    ),
)
def test_availability_saves_desired_state_and_queues_real_operation(
    client, admin, site, initial, target
):
    site.availability = initial
    site.save()
    client.force_login(admin)
    with patch.object(management_views.apply_availability, "delay") as delay:
        response = client.post(
            availability_url(site), {"availability": target, "confirmation": site.name}
        )
    assert response.url == reverse("sites:dashboard", args=[site.pk])
    site.refresh_from_db()
    assert site.availability == target
    operation = Operation.objects.get(site=site)
    assert operation.ty == "update_availability"
    assert operation.status == "queued"
    delay.assert_called_once_with(operation.pk)


def test_availability_get_never_changes_site_or_queues_operation(client, admin, site):
    client.force_login(admin)
    with patch.object(management_views.apply_availability, "delay") as delay:
        response = client.get(
            availability_url(site), {"availability": "disabled", "confirmation": site.name}
        )
    assert response.status_code == 200
    site.refresh_from_db()
    assert site.availability == "enabled"
    assert not Operation.objects.exists()
    delay.assert_not_called()


def test_availability_requires_csrf(admin, site):
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin)
    assert (
        client.post(
            availability_url(site), {"availability": "disabled", "confirmation": site.name}
        ).status_code
        == 403
    )
    site.refresh_from_db()
    assert site.availability == "enabled"


def test_availability_does_not_replace_existing_operation(client, admin, site):
    operation = site.start_operation("restart_site")
    client.force_login(admin)
    with patch.object(management_views.apply_availability, "delay") as delay:
        response = client.post(
            availability_url(site), {"availability": "disabled", "confirmation": site.name}
        )
    assert response.status_code == 200
    assert response.context["form"].non_field_errors()
    assert response.context["site"].availability == "enabled"
    site.refresh_from_db()
    assert site.availability == "enabled"
    assert Operation.objects.get(site=site).pk == operation.pk
    delay.assert_not_called()


def test_availability_queue_failure_retains_desired_state_and_retryable_operation(
    client, admin, site
):
    client.force_login(admin)
    with patch.object(
        management_views.apply_availability, "delay", side_effect=RuntimeError("queue offline")
    ):
        response = client.post(
            availability_url(site), {"availability": "disabled", "confirmation": site.name}
        )
    assert response.status_code == 302
    site.refresh_from_db()
    assert site.availability == "disabled"
    operation = Operation.objects.get(site=site)
    assert operation.status == "failed"
    assert operation.action_set.get().slug == "queue_operation"
    with patch.object(management_views.apply_availability, "delay") as delay:
        client.post(
            reverse("sites:operation-reset", args=[operation.pk]), {"confirmation": site.name}
        )
    replacement = Operation.objects.get(site=site)
    assert replacement.pk != operation.pk
    assert replacement.ty == "update_availability"
    assert replacement.status == "queued"
    delay.assert_called_once_with(replacement.pk)


@pytest.mark.parametrize("availability", ("enabled", "not-served", "disabled"))
def test_availability_task_updates_public_route_or_removes_service(site, availability):
    site.availability = availability
    site.save()
    operation = site.start_operation("update_availability")
    appserver = Appserver("mocked-appserver")
    expected_path = (
        "/api/docker/service/remove" if availability == "disabled" else "/api/docker/service/update"
    )
    captured = []

    def response_callback(request):
        captured.append(json.loads(request.body))
        return 200, {"Content-Type": "application/json"}, '{"ok": true}'

    with responses.RequestsMock() as remote:
        remote.add_callback(
            "POST",
            f"{Appserver.protocol()}://{appserver.host}{expected_path}",
            callback=response_callback,
        )
        with patch.object(Appserver, "list_pingable", return_value=[appserver]):
            management_views.apply_availability(operation.pk)
    assert captured == [site.serialize_for_appserver()]
    assert captured[0]["is_served"] is (availability == "enabled")
    assert not Operation.objects.exists()


def test_availability_remote_failure_retains_lock_and_desired_state(site):
    site.availability = "disabled"
    site.save()
    operation = site.start_operation("update_availability")
    with framework.mock(
        {"path": "/api/docker/service/remove", "data": {"error": "offline"}, "status_code": 500}
    ):
        management_views.apply_availability(operation.pk)
    operation.refresh_from_db()
    assert operation.status == "failed"
    assert operation.action_set.get().user_message
    site.refresh_from_db()
    assert site.availability == "disabled"


@pytest.mark.parametrize("started", (False, True))
def test_reset_refuses_queued_or_running_operation(client, admin, site, started):
    operation = site.start_operation("restart_site")
    if started:
        operation.started_time = timezone.now()
        operation.save(update_fields=["started_time"])
    client.force_login(admin)
    with patch.object(tasks.restart_site_process, "delay") as delay:
        response = client.post(
            reverse("sites:operation-reset", args=[operation.pk]), {"confirmation": site.name}
        )
    assert response.url == reverse("sites:management")
    assert Operation.objects.get(site=site).pk == operation.pk
    delay.assert_not_called()


@pytest.mark.parametrize("confirmation", ("", "wrong", "TEST-SITE", " test-site", "test-site "))
def test_reset_requires_exact_confirmation_before_dispatch(client, admin, site, confirmation):
    operation = failed_operation(site, "restart_site")
    client.force_login(admin)
    with patch.object(tasks.restart_site_process, "delay") as delay:
        response = client.post(
            reverse("sites:operation-reset", args=[operation.pk]), {"confirmation": confirmation}
        )
    assert response.url == reverse("sites:management")
    assert Operation.objects.get(site=site).pk == operation.pk
    assert operation.action_set.count() == 1
    delay.assert_not_called()


@pytest.mark.parametrize("kind", ("restart_site", "update_availability", "regen_nginx_config"))
def test_reset_dispatches_matching_failed_operation_and_clears_old_actions(
    client, admin, site, kind
):
    operation = failed_operation(site, kind)
    task = {
        "restart_site": tasks.restart_site_process,
        "update_availability": management_views.apply_availability,
        "regen_nginx_config": settings_tasks.apply_site_settings,
    }[kind]
    client.force_login(admin)
    with patch.object(task, "delay") as delay:
        response = client.post(
            reverse("sites:operation-reset", args=[operation.pk]), {"confirmation": site.name}
        )
    assert response.url == reverse("sites:dashboard", args=[site.pk])
    replacement = Operation.objects.get(site=site)
    assert replacement.pk != operation.pk
    assert replacement.ty == kind
    assert replacement.status == "queued"
    assert not Action.objects.exists()
    delay.assert_called_once_with(replacement.pk)


def test_reset_unknown_operation_kind_leaves_failure_for_inspection(client, admin, site):
    operation = failed_operation(site, "unknown_legacy_kind")
    client.force_login(admin)
    response = client.post(
        reverse("sites:operation-reset", args=[operation.pk]), {"confirmation": site.name}
    )
    assert response.status_code == 302
    assert Operation.objects.get(site=site).pk == operation.pk
    assert operation.action_set.count() == 1


def test_reset_requires_post_and_csrf(admin, site):
    operation = failed_operation(site)
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin)
    endpoint = reverse("sites:operation-reset", args=[operation.pk])
    assert client.get(endpoint).status_code == 405
    assert client.post(endpoint, {"confirmation": site.name}).status_code == 403
    assert Operation.objects.get(site=site).pk == operation.pk


def test_reset_stale_operation_id_does_not_retry_replacement(client, admin, site):
    old = failed_operation(site, "restart_site")
    old_id = old.pk
    old.action_set.all().delete()
    old.delete()
    replacement = failed_operation(site, "regen_nginx_config")
    client.force_login(admin)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        assert (
            client.post(
                reverse("sites:operation-reset", args=[old_id]),
                {"confirmation": site.name},
            ).status_code
            == 404
        )
    assert Operation.objects.get(site=site).pk == replacement.pk
    delay.assert_not_called()


def test_reset_does_not_dispatch_a_new_failure_if_target_changes_before_retry(client, admin, site):
    old = failed_operation(site, "restart_site")
    original_retry = management_views.retry_operation
    changed_operation = None

    def change_before_dispatch(request, site_id, *args, **kwargs):
        nonlocal changed_operation
        old.action_set.all().delete()
        old.delete()
        changed_operation = failed_operation(site, "delete_site")
        return original_retry(request, site_id, *args, **kwargs)

    client.force_login(admin)
    with (
        patch.object(management_views, "retry_operation", side_effect=change_before_dispatch),
        patch.object(tasks.delete_site, "delay") as delete_delay,
        patch.object(tasks.restart_site_process, "delay") as restart_delay,
    ):
        response = client.post(
            reverse("sites:operation-reset", args=[old.pk]), {"confirmation": site.name}
        )
    assert response.status_code == 302
    assert Operation.objects.get(site=site).pk == changed_operation.pk
    assert changed_operation.action_set.count() == 1
    delete_delay.assert_not_called()
    restart_delay.assert_not_called()


@pytest.mark.parametrize("action", ("operation-reset", "retry"))
def test_retry_does_not_replace_changed_failure_between_read_and_locked_start(
    client, admin, site, action
):
    old = failed_operation(site, "restart_site")
    original_start = Site.start_operation
    changed_operation = None

    def change_before_locked_start(instance, kind, **kwargs):
        nonlocal changed_operation
        old.action_set.all().delete()
        old.delete()
        changed_operation = Operation.objects.create(site=instance, ty="delete_site")
        Action.objects.create(
            operation=changed_operation,
            slug="failed_delete",
            name="Failed deletion",
            result=False,
        )
        return original_start(instance, kind, **kwargs)

    target = old.pk if action == "operation-reset" else site.pk
    client.force_login(admin)
    with (
        patch.object(Site, "start_operation", change_before_locked_start),
        patch.object(tasks.restart_site_process, "delay") as restart_delay,
        patch.object(tasks.delete_site, "delay") as delete_delay,
    ):
        response = client.post(
            reverse("sites:" + action, args=[target]), {"confirmation": site.name}
        )
    assert response.status_code == 302
    assert Operation.objects.get(site=site).pk == changed_operation.pk
    assert changed_operation.action_set.count() == 1
    restart_delay.assert_not_called()
    delete_delay.assert_not_called()


@pytest.mark.parametrize("actor", ("member", "admin"))
def test_reapply_configuration_queues_for_authorized_site(client, student, admin, site, actor):
    client.force_login(admin if actor == "admin" else student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(reverse("sites:configuration-reapply", args=[site.pk]))
    assert response.url == reverse("sites:dashboard", args=[site.pk])
    operation = Operation.objects.get(site=site)
    assert operation.ty == "regen_nginx_config"
    delay.assert_called_once_with(operation.pk)


def test_reapply_configuration_cannot_access_other_users_site(client, teacher, site):
    client.force_login(teacher)
    assert client.post(reverse("sites:configuration-reapply", args=[site.pk])).status_code == 404
    assert not Operation.objects.exists()


def test_reapply_disabled_site_requires_administrator(client, student, admin, site):
    site.availability = "disabled"
    site.save()
    endpoint = reverse("sites:configuration-reapply", args=[site.pk])
    client.force_login(student)
    assert client.post(endpoint).status_code == 404
    client.force_login(admin)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        assert client.post(endpoint).status_code == 302
    delay.assert_called_once_with(Operation.objects.get(site=site).pk)


def test_reapply_requires_post_csrf_and_keeps_existing_operation(student, site):
    operation = site.start_operation("restart_site")
    client = Client(enforce_csrf_checks=True)
    client.force_login(student)
    endpoint = reverse("sites:configuration-reapply", args=[site.pk])
    assert client.get(endpoint).status_code == 405
    assert client.post(endpoint).status_code == 403
    client = Client()
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        assert client.post(endpoint).status_code == 302
    assert Operation.objects.get(site=site).pk == operation.pk
    delay.assert_not_called()


def test_metrics_deny_anonymous_and_nonadmin_by_default(client, student, settings):
    settings.DIRECTOR_METRICS_SCRAPE_IPS = []
    endpoint = reverse("sites:metrics")
    assert client.get(endpoint, REMOTE_ADDR="192.0.2.10").status_code == 403
    student.is_staff = True
    student.save(update_fields=["is_staff"])
    client.force_login(student)
    assert client.get(endpoint, REMOTE_ADDR="192.0.2.10").status_code == 403


def test_metrics_allow_configured_remote_ip_and_count_only_unrecoverable_failures(
    client, site, settings
):
    settings.DIRECTOR_METRICS_SCRAPE_IPS = ["192.0.2.10"]
    operation = site.start_operation("restart_site")
    for index, (result, recoverable) in enumerate(
        ((False, False), (False, False), (False, True), (None, False), (True, False))
    ):
        Action.objects.create(
            operation=operation,
            slug=f"action_{chr(97 + index)}",
            name="Action",
            result=result,
            user_recoverable=recoverable,
        )
    response = client.get(reverse("sites:metrics"), REMOTE_ADDR="192.0.2.10")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain; version=0.0.4")
    assert (
        response.content
        == b"# TYPE director_sites_failed_actions gauge\ndirector_sites_failed_actions 2\n"
    )


@pytest.mark.parametrize(
    "headers",
    (
        {"HTTP_X_FORWARDED_FOR": "192.0.2.10"},
        {"HTTP_FORWARDED": "for=192.0.2.10"},
        {"HTTP_X_REAL_IP": "192.0.2.10"},
        {"HTTP_X_FORWARDED_FOR": "192.0.2.10, 198.51.100.4"},
    ),
)
def test_metrics_ignore_spoofed_forwarding_headers(client, settings, headers):
    settings.DIRECTOR_METRICS_SCRAPE_IPS = ["192.0.2.10"]
    response = client.get(reverse("sites:metrics"), REMOTE_ADDR="198.51.100.4", **headers)
    assert response.status_code == 403


def test_superuser_can_read_metrics_from_unlisted_ip(client, admin, settings):
    settings.DIRECTOR_METRICS_SCRAPE_IPS = []
    client.force_login(admin)
    response = client.get(reverse("sites:metrics"), REMOTE_ADDR="198.51.100.4")
    assert response.status_code == 200
    assert b"director_sites_failed_actions 0" in response.content
