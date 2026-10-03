"""Verify site settings permissions, domain ownership, and deployment lifecycle."""

from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from .. import settings_tasks
from ..models import Action, Domain, Operation, Site
from ..settings_forms import AddDomainForm, SiteNameForm
from . import framework


@pytest.fixture(autouse=True)
def memory_channels(settings):
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


@pytest.fixture
def site(student):
    result = Site.objects.create(name="test-site", mode="static", purpose="project")
    result.users.add(student)
    return result


@pytest.fixture
def admin(django_user_model):
    return django_user_model.objects.create_user(
        username="administrator", is_superuser=True, is_staff=True, accepted_guidelines=True
    )


def endpoint(action, site, *args):
    return reverse("sites:settings" + ("-" + action if action else ""), args=[site.id, *args])


def failed_operation(site, kind):
    operation = site.start_operation(kind)
    Action.objects.create(operation=operation, slug="failed-step", name="Failed step", result=False)
    return operation


@pytest.mark.parametrize("action", ("", "meta", "name", "type", "member-add", "domain-add"))
def test_other_user_cannot_read_or_change_settings(client, teacher, site, action):
    client.force_login(teacher)
    response = client.post(endpoint(action, site)) if action else client.get(endpoint(action, site))
    assert response.status_code == 404
    assert not Operation.objects.exists()


@pytest.mark.parametrize("action", ("meta", "name", "type", "admin", "member-add", "domain-add"))
def test_mutations_require_post(client, student, site, action):
    client.force_login(student)
    assert client.get(endpoint(action, site)).status_code == 405


def test_settings_mutation_requires_csrf(student, site):
    client = Client(enforce_csrf_checks=True)
    client.force_login(student)
    assert client.post(endpoint("meta", site), {"description": "Changed"}).status_code == 403
    site.refresh_from_db()
    assert site.description == ""


def test_disabled_site_has_read_only_settings_and_comments(client, student, site):
    site.availability = "disabled"
    site.admin_comments = "Please contact the hosting team."
    site.save()
    client.force_login(student)
    response = client.get(endpoint("", site))
    assert response.status_code == 200
    assert response.context["can_edit"] is False
    assert site.admin_comments.encode() in response.content
    assert b'id="settings-meta-form"' not in response.content
    assert client.post(endpoint("meta", site), {"description": "Changed"}).status_code == 404


def test_description_is_saved_without_deployment_and_purpose_tamper_is_ignored(
    client, student, site
):
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(
            endpoint("meta", site), {"description": "New description", "purpose": "user"}
        )
    assert response.url == endpoint("", site)
    site.refresh_from_db()
    assert site.description == "New description"
    assert site.purpose == "project"
    assert not Operation.objects.exists()
    delay.assert_not_called()


def test_admin_can_change_purpose_with_deployment(client, admin, site):
    client.force_login(admin)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(
            endpoint("meta", site), {"description": "Changed", "purpose": "activity"}
        )
    assert response.status_code == 302
    site.refresh_from_db()
    assert site.purpose == "activity"
    assert site.operation.ty == "edit_site_names"
    delay.assert_called_once_with(site.operation.id)


@pytest.mark.parametrize("htmx", (False, True))
def test_invalid_rename_keeps_saved_heading_and_bound_input(client, student, site, htmx):
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(
            endpoint("name", site),
            {"name": "UPPERCASE"},
            headers={"HX-Request": "true"} if htmx else {},
        )
    assert response.status_code == 200
    form = response.context["form"] if htmx else response.context["name_form"]
    assert form["name"].value() == "UPPERCASE"
    assert form.errors["name"]
    assert response.context["site"].name == "test-site"
    site.refresh_from_db()
    assert site.name == "test-site"
    assert not Operation.objects.exists()
    delay.assert_not_called()


def test_rename_saves_desired_name_and_queues_routing(client, student, site):
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(endpoint("name", site), {"name": "renamed-site"})
    assert response.url == endpoint("", site)
    site.refresh_from_db()
    assert site.name == "renamed-site"
    assert site.operation.ty == "rename_site"
    delay.assert_called_once_with(site.operation.id)


def test_existing_operation_rolls_back_deployment_change(client, student, site):
    operation = site.start_operation("restart_site")
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(endpoint("name", site), {"name": "renamed-site"})
    assert response.status_code == 200
    assert response.context["name_form"].non_field_errors()
    site.refresh_from_db()
    assert site.name == "test-site"
    assert site.operation.id == operation.id
    delay.assert_not_called()


def test_description_and_members_can_change_during_deployment(client, student, teacher, site):
    site.start_operation("restart_site")
    client.force_login(student)
    assert client.post(endpoint("meta", site), {"description": "Changed"}).status_code == 302
    assert (
        client.post(endpoint("member-add", site), {"username": teacher.username}).status_code == 302
    )
    site.refresh_from_db()
    assert site.description == "Changed"
    assert site.users.filter(pk=teacher.pk).exists()


def test_personal_site_name_cannot_be_changed_even_by_admin(client, admin, site):
    site.purpose = "user"
    site.save()
    client.force_login(admin)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        client.post(endpoint("name", site), {"name": "different-name"})
    site.refresh_from_db()
    assert site.name == "test-site"
    assert not Operation.objects.exists()
    delay.assert_not_called()


@pytest.mark.parametrize("name", ("9project", "reserved", "service-internal", "admin-only"))
def test_rename_obeys_name_policy(student, site, settings, name):
    settings.BLACKLISTED_SITE_NAMES = ["reserved"]
    settings.BLACKLISTED_SITE_REGEXES = [r"-internal$"]
    settings.FORBIDDEN_SITE_NAME_REGEX = r"^admin-"
    form = SiteNameForm({"name": name}, instance=site, user=student)
    assert not form.is_valid()
    assert form.errors["name"]


def test_new_name_must_be_unique(student, site):
    Site.objects.create(name="already-used", mode="static", purpose="project")
    form = SiteNameForm({"name": "already-used"}, instance=site, user=student)
    assert not form.is_valid()
    assert form.errors["name"]


def test_admin_cannot_rename_project_to_number_leading_name(admin, site):
    form = SiteNameForm({"name": "9project"}, instance=site, user=admin)
    assert not form.is_valid()
    assert form.errors["name"]


def test_rename_database_conflict_rolls_back_operation_and_name(client, student, site):
    client.force_login(student)
    with (
        patch.object(SiteNameForm, "save", side_effect=IntegrityError("name already claimed")),
        patch.object(settings_tasks.apply_site_settings, "delay") as delay,
    ):
        response = client.post(endpoint("name", site), {"name": "renamed-site"})
    assert response.status_code == 200
    assert response.context["name_form"].non_field_errors()
    assert response.context["name_form"]["name"].value() == "renamed-site"
    site.refresh_from_db()
    assert site.name == "test-site"
    assert not Operation.objects.exists()
    delay.assert_not_called()


def test_member_addition_resolves_local_username_case_insensitively(client, student, teacher, site):
    client.force_login(student)
    response = client.post(endpoint("member-add", site), {"username": "TEACHER"})
    assert response.url == endpoint("", site)
    assert site.users.filter(pk=teacher.pk).exists()


def test_member_invitation_is_sent_only_after_membership_commit(
    client, student, teacher, site, django_capture_on_commit_callbacks
):
    client.force_login(student)
    with patch("director.apps.sites.settings_views.notify_site_member_added") as notify:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(endpoint("member-add", site), {"username": teacher.username})
            notify.assert_not_called()
            assert site.users.filter(pk=teacher.pk).exists()
        notify.assert_called_once_with(site, teacher, student)
    assert response.status_code == 302


def test_rejected_member_addition_sends_no_invitation(
    client, student, site, django_capture_on_commit_callbacks
):
    client.force_login(student)
    with patch("director.apps.sites.settings_views.notify_site_member_added") as notify:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(endpoint("member-add", site), {"username": "unknown"})
        notify.assert_not_called()
    assert response.status_code == 200


@pytest.mark.parametrize("username", ("unknown", "student"))
def test_unknown_or_existing_member_is_rejected(client, student, site, username):
    client.force_login(student)
    response = client.post(endpoint("member-add", site), {"username": username})
    assert response.status_code == 200
    assert response.context["member_form"].errors["username"]
    assert site.users.count() == 1


def test_inactive_member_cannot_be_added(client, student, teacher, site):
    teacher.is_active = False
    teacher.save()
    client.force_login(student)
    response = client.post(endpoint("member-add", site), {"username": teacher.username})
    assert response.context["member_form"].errors["username"]


@pytest.mark.parametrize("actor", ("member", "admin"))
def test_last_member_cannot_be_removed(client, student, admin, site, actor):
    client.force_login(admin if actor == "admin" else student)
    response = client.post(endpoint("member-remove", site, student.id))
    assert response.url == endpoint("", site)
    assert list(site.users.all()) == [student]


def test_self_removal_redirects_and_revokes_access(client, student, teacher, site):
    site.users.add(teacher)
    client.force_login(student)
    response = client.post(endpoint("member-remove", site, student.id))
    assert response.url == reverse("sites:index")
    assert client.get(endpoint("", site)).status_code == 404
    assert list(site.users.all()) == [teacher]


def test_remove_member_cannot_target_a_user_outside_site(client, student, teacher, site):
    client.force_login(student)
    assert client.post(endpoint("member-remove", site, teacher.id)).status_code == 404


@pytest.mark.parametrize(
    "domain",
    (
        "https://example.org",
        "example.org/path",
        "example.org:443",
        "127.0.0.1",
        "a..org",
        "-bad.org",
        "bad-.org",
        "éxample.org",
        "a" * 64 + ".org",
    ),
)
def test_domain_rejects_invalid_hostname(student, site, domain):
    form = AddDomainForm({"domain": domain}, site=site, user=student)
    assert not form.is_valid()
    assert form.errors["domain"]


@pytest.mark.parametrize(
    "domain",
    (
        "sites.tjhsst.edu",
        "anything.sites.tjhsst.edu",
        "sites.localhost",
        "anything.sites.localhost",
    ),
)
@pytest.mark.parametrize("actor", ("member", "admin"))
def test_generated_site_zones_are_reserved_for_all(student, admin, site, domain, actor):
    form = AddDomainForm({"domain": domain}, site=site, user=admin if actor == "admin" else student)
    assert not form.is_valid()


def test_school_domains_require_administrator_and_suffix_match_is_exact(student, admin, site):
    assert not AddDomainForm({"domain": "school.tjhsst.edu"}, site=site, user=student).is_valid()
    assert AddDomainForm({"domain": "school.tjhsst.edu"}, site=site, user=admin).is_valid()
    assert AddDomainForm({"domain": "nottjhsst.edu"}, site=site, user=student).is_valid()


@pytest.mark.parametrize("status", ("active", "inactive", "blocked", "removing"))
def test_hostname_ownership_cannot_be_stolen(student, teacher, site, status):
    other = Site.objects.create(name="other-site", mode="static", purpose="project")
    Domain.objects.create(
        site=None if status == "blocked" else other, domain="EXAMPLE.ORG", status=status
    )
    form = AddDomainForm({"domain": "example.org"}, site=site, user=student)
    assert not form.is_valid()
    assert form.errors["domain"]


def test_database_constraint_enforces_case_insensitive_live_hostname(site):
    Domain.objects.create(site=site, domain="EXAMPLE.ORG", status="removing")
    with pytest.raises(IntegrityError), transaction.atomic():
        Domain.objects.create(site=site, domain="example.org")


def test_domain_add_normalizes_and_queues_routing(client, student, site):
    Domain.objects.create(domain="example.org", status="deleted")
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(endpoint("domain-add", site), {"domain": " Example.ORG. "})
    assert response.url == endpoint("", site)
    domain = Domain.objects.get(site=site)
    assert domain.domain == "example.org"
    assert domain.creating_user == student
    assert domain.status == "active"
    assert site.operation.ty == "edit_site_names"
    delay.assert_called_once_with(site.operation.id)


def test_owned_inactive_domain_is_reactivated(client, student, site):
    domain = Domain.objects.create(site=site, domain="example.org", status="inactive")
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay"):
        client.post(endpoint("domain-add", site), {"domain": "example.org"})
    domain.refresh_from_db()
    assert domain.status == "active"
    assert Domain.objects.count() == 1


def test_domain_change_during_operation_is_not_saved(client, student, site):
    site.start_operation("restart_site")
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(endpoint("domain-add", site), {"domain": "example.org"})
    assert response.status_code == 200
    assert response.context["domain_form"].non_field_errors()
    assert not Domain.objects.exists()
    delay.assert_not_called()


def test_domain_race_rolls_back_operation_and_preserves_bound_error(client, student, site):
    client.force_login(student)
    with (
        patch.object(
            Domain.objects, "create", side_effect=IntegrityError("hostname already claimed")
        ),
        patch.object(settings_tasks.apply_site_settings, "delay") as delay,
    ):
        response = client.post(
            endpoint("domain-add", site), {"domain": "example.org"}, headers={"HX-Request": "true"}
        )
    assert response.status_code == 200
    assert response.context["form"].errors["domain"]
    assert response.context["form"]["domain"].value() == "example.org"
    assert not Operation.objects.exists()
    assert not Domain.objects.exists()
    delay.assert_not_called()


def test_domain_removal_is_queued_and_keeps_ownership(client, student, site):
    domain = Domain.objects.create(site=site, domain="example.org")
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(endpoint("domain-remove", site, domain.id))
    assert response.url == endpoint("", site)
    domain.refresh_from_db()
    assert domain.status == "removing"
    assert domain.site == site
    assert "https://example.org" not in site.list_domains()
    delay.assert_called_once_with(site.operation.id)


def test_domain_removal_cannot_target_other_site(client, student, site):
    other = Site.objects.create(name="other-site", mode="static", purpose="project")
    domain = Domain.objects.create(site=other, domain="example.org")
    client.force_login(student)
    assert client.post(endpoint("domain-remove", site, domain.id)).status_code == 404
    assert not Operation.objects.exists()


def test_queue_failure_retains_changed_setting_and_can_be_retried(client, student, site):
    client.force_login(student)
    with patch.object(
        settings_tasks.apply_site_settings, "delay", side_effect=RuntimeError("queue down")
    ):
        response = client.post(endpoint("name", site), {"name": "renamed-site"})
    assert response.status_code == 302
    site.refresh_from_db()
    assert site.name == "renamed-site"
    failed = site.operation
    assert failed.status == "failed"
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(reverse("sites:retry", args=[site.id]))
    replacement = Operation.objects.get(site=site)
    assert replacement.pk != failed.pk
    assert replacement.ty == "rename_site"
    assert replacement.status == "queued"
    delay.assert_called_once_with(replacement.pk)


def test_domain_released_only_after_successful_remote_update(site):
    domain = Domain.objects.create(site=site, domain="example.org", status="removing")
    operation = site.start_operation("edit_site_names")
    with framework.mock({"path": "/api/docker/service/update", "data": {"ok": True}}):
        settings_tasks.apply_site_settings(operation.id)
    domain.refresh_from_db()
    assert domain.status == "deleted"
    assert domain.site_id is None
    assert not Operation.objects.exists()


def test_failed_remote_update_keeps_domain_ownership_and_operation(site):
    domain = Domain.objects.create(site=site, domain="example.org", status="removing")
    operation = site.start_operation("edit_site_names")
    with framework.mock(
        {"path": "/api/docker/service/update", "data": {"error": "offline"}, "status_code": 500}
    ):
        settings_tasks.apply_site_settings(operation.id)
    domain.refresh_from_db()
    assert domain.status == "removing"
    assert domain.site == site
    operation.refresh_from_db()
    assert operation.status == "failed"
    assert operation.action_set.get(slug="release_removed_domains").result is None


def test_type_change_builds_image_before_updating_service(site):
    site.mode = "dynamic"
    site.save()
    operation = site.start_operation("change_site_type")
    called = []

    def build_image(_site, _appservers):
        called.append("build")
        yield "Image built"

    def update_service(_site, _appservers):
        called.append("update")
        yield "Service updated"

    with (
        patch("director.apps.sites.appserver.Appserver.list_pingable", return_value=[]),
        patch.object(settings_tasks.actions, "build_docker_image", build_image),
        patch.object(settings_tasks.actions, "update_docker_service", update_service),
    ):
        settings_tasks.apply_site_settings(operation.id)
    assert called == ["build", "update"]
    assert not Operation.objects.exists()


def test_type_change_saves_desired_mode_with_operation(client, student, site):
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        client.post(endpoint("type", site), {"mode": "dynamic"})
    site.refresh_from_db()
    assert site.mode == "dynamic"
    assert site.operation.ty == "change_site_type"
    delay.assert_called_once_with(site.operation.id)


def test_member_cannot_edit_admin_fields(client, student, site):
    client.force_login(student)
    assert (
        client.post(
            endpoint("admin", site), {"admin_comments": "Changed", "custom_nginx_config": "x"}
        ).status_code
        == 403
    )
    site.refresh_from_db()
    assert site.admin_comments == ""
    assert site.custom_nginx_config == ""
    assert not Operation.objects.exists()


def test_admin_comment_only_does_not_deploy_and_config_change_does(client, admin, site):
    site.availability = "disabled"
    site.save()
    client.force_login(admin)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(
            endpoint("admin", site), {"admin_comments": "Hosting paused", "custom_nginx_config": ""}
        )
    assert response.url == endpoint("", site)
    site.refresh_from_db()
    assert site.admin_comments == "Hosting paused"
    assert not Operation.objects.exists()
    delay.assert_not_called()
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        client.post(
            endpoint("admin", site),
            {
                "admin_comments": "Hosting paused",
                "custom_nginx_config": "location / { return 200; }",
            },
        )
    site.refresh_from_db()
    assert site.custom_nginx_config == "location / { return 200; }"
    assert site.operation.ty == "regen_nginx_config"
    delay.assert_called_once_with(site.operation.id)


@pytest.mark.parametrize("previous_kind", ("rename_site", "edit_site_names", "regen_nginx_config"))
def test_failed_routing_settings_accept_a_corrected_configuration(
    client, admin, site, previous_kind
):
    site.custom_nginx_config = "invalid configuration"
    site.save()
    domain = Domain.objects.create(site=site, domain="old.example.org", status="removing")
    old_operation = failed_operation(site, previous_kind)
    client.force_login(admin)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(
            endpoint("admin", site),
            {"admin_comments": "", "custom_nginx_config": "location / { return 200; }"},
        )
    assert response.status_code == 302
    site.refresh_from_db()
    replacement = Operation.objects.get(site=site)
    assert replacement.pk != old_operation.pk
    assert replacement.ty == "regen_nginx_config"
    assert replacement.status == "queued"
    assert site.custom_nginx_config == "location / { return 200; }"
    assert not Action.objects.exists()
    domain.refresh_from_db()
    assert domain.site == site
    assert domain.status == "removing"
    delay.assert_called_once_with(replacement.pk)


def test_failed_type_change_can_be_corrected_by_reverting_type(client, student, site):
    site.mode = "dynamic"
    site.save()
    old_operation = failed_operation(site, "change_site_type")
    client.force_login(student)
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(endpoint("type", site), {"mode": "static"})
    assert response.status_code == 302
    site.refresh_from_db()
    replacement = Operation.objects.get(site=site)
    assert replacement.pk != old_operation.pk
    assert replacement.ty == "change_site_type"
    assert site.mode == "static"
    delay.assert_called_once_with(replacement.pk)


@pytest.mark.parametrize(
    ("previous_kind", "action", "data"),
    (
        ("create_site", "name", {"name": "renamed-site"}),
        ("delete_site", "domain-add", {"domain": "example.org"}),
        ("rename_site", "type", {"mode": "dynamic"}),
        ("change_site_type", "name", {"name": "renamed-site"}),
    ),
)
def test_correction_cannot_replace_an_unrelated_failed_operation(
    client, site, previous_kind, action, data
):
    old_operation = failed_operation(site, previous_kind)
    client.force_login(site.users.get())
    with patch.object(settings_tasks.apply_site_settings, "delay") as delay:
        response = client.post(endpoint(action, site), data)
    assert response.status_code == 200
    site.refresh_from_db()
    assert site.operation.pk == old_operation.pk
    assert site.name == "test-site"
    assert site.mode == "static"
    assert not Domain.objects.exists()
    delay.assert_not_called()
