"""Image catalog permissions, queued customization, and real resource deployment."""

from unittest.mock import Mock, patch

import pytest
from django.urls import reverse

from .. import actions, images_tasks
from ..appserver import Appserver
from ..images import image_setup_for_site
from ..images_forms import ImageSelectForm, SiteResourceLimitsForm
from ..models import (
    Action,
    DockerImage,
    DockerImageSetupCommand,
    Operation,
    Site,
    SiteResourceLimits,
)
from . import framework


@pytest.fixture(autouse=True)
def memory_channels(settings):
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


@pytest.fixture
def administrator(django_user_model):
    return django_user_model.objects.create_user(
        username="image-admin", is_staff=True, is_superuser=True, accepted_guidelines=True
    )


@pytest.fixture
def site(student):
    site = Site.objects.create(name="image-site", mode="dynamic", purpose="project")
    site.users.add(student)
    return site


@pytest.fixture
def image():
    return DockerImage.objects.create(
        name="python:3.13-alpine",
        friendly_name="Python",
        is_user_visible=True,
        install_command_prefix="pip install",
        run_script_template="#!/bin/sh\npython app.py\n",
    )


@pytest.mark.parametrize("name", ("image-catalog", "image-create", "setup-create", "resource-list"))
def test_catalog_and_admin_controls_forbid_non_admin(client, student, name):
    client.force_login(student)
    assert client.get(reverse(f"sites:{name}")).status_code == 403
    assert client.post(reverse(f"sites:{name}"), {}).status_code == 403


def test_admin_catalog_crud_preserves_setup_links_and_immutable_reference(client, administrator):
    client.force_login(administrator)
    response = client.post(
        reverse("sites:setup-create"),
        {"name": "Prepare Python", "command": "python -m ensurepip", "order": 2},
    )
    assert response.status_code == 302
    command = DockerImageSetupCommand.objects.get()
    payload = {
        "name": "python:3.13-alpine",
        "friendly_name": "Python",
        "description": "Python runtime",
        "is_user_visible": "on",
        "setup_commands": [command.pk],
        "install_command_prefix": "pip install",
    }
    assert client.post(reverse("sites:image-create"), payload).status_code == 302
    image = DockerImage.objects.get(name=payload["name"])
    assert list(image.setup_commands.all()) == [command]
    payload["name"] = "attacker:latest"
    payload["friendly_name"] = "Python 3.13"
    assert client.post(reverse("sites:image-edit", args=[image.pk]), payload).status_code == 302
    image.refresh_from_db()
    assert image.name == "python:3.13-alpine"
    assert image.friendly_name == "Python 3.13"
    assert (
        client.post(
            reverse("sites:image-delete", args=[image.pk]), {"confirmation": "wrong"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            reverse("sites:image-delete", args=[image.pk]), {"confirmation": image.name}
        ).status_code
        == 302
    )
    assert not DockerImage.objects.filter(pk=image.pk).exists()
    assert (
        client.post(
            reverse("sites:setup-delete", args=[command.pk]), {"confirmation": command.name}
        ).status_code
        == 302
    )
    assert not DockerImageSetupCommand.objects.exists()


def test_used_image_cannot_be_deleted(client, administrator, site, image):
    site.docker_image = image
    site.save()
    client.force_login(administrator)
    assert (
        client.post(
            reverse("sites:image-delete", args=[image.pk]), {"confirmation": image.name}
        ).status_code
        == 200
    )
    assert DockerImage.objects.filter(pk=image.pk).exists()


def test_owners_only_and_disabled_sites_cannot_customize(client, student, teacher, site, image):
    client.force_login(teacher)
    assert client.get(reverse("sites:image-select", args=[site.pk])).status_code == 404
    client.force_login(student)
    site.availability = "disabled"
    site.save()
    assert (
        client.post(reverse("sites:image-select", args=[site.pk]), {"image": image.pk}).status_code
        == 404
    )


def test_hidden_images_cannot_be_selected(client, student, site, image):
    image.is_user_visible = False
    image.save()
    client.force_login(student)
    with patch.object(images_tasks.rebuild_image, "delay") as delay:
        response = client.post(reverse("sites:image-select", args=[site.pk]), {"image": image.pk})
    assert response.status_code == 200
    assert response.context["form"].errors["image"]
    site.refresh_from_db()
    assert site.docker_image_id is None
    delay.assert_not_called()


@pytest.mark.parametrize(
    "packages", ("requests;id", "$(id)", "-r requirements.txt", "../outside", "a" * 101)
)
def test_package_shell_and_option_injection_is_rejected(student, site, image, packages):
    form = ImageSelectForm({"image": image.pk, "packages": packages}, site=site, user=student)
    assert not form.is_valid()
    assert "packages" in form.errors


def test_selected_image_and_packages_queue_build_and_keep_overwrite_intent(
    client, student, site, image
):
    client.force_login(student)
    with patch.object(images_tasks.rebuild_image, "delay") as delay:
        response = client.post(
            reverse("sites:image-select", args=[site.pk]),
            {
                "image": image.pk,
                "packages": "requests==2.32.3 requests==2.32.3 pytest",
                "write_run_sh_file": "on",
            },
        )
    assert response.url == reverse("sites:dashboard", args=[site.pk])
    site.refresh_from_db()
    assert site.docker_image == image
    assert site.image_packages == "requests==2.32.3 pytest"
    assert site.image_write_run_script
    assert site.operation.ty == "update_docker_image"
    delay.assert_called_once_with(site.operation.pk)


def test_busy_site_keeps_previous_image_configuration(client, student, site, image):
    operation = site.start_operation("restart_site")
    client.force_login(student)
    with patch.object(images_tasks.rebuild_image, "delay") as delay:
        response = client.post(
            reverse("sites:image-select", args=[site.pk]),
            {"image": image.pk, "packages": "requests"},
        )
    assert response.status_code == 200
    site.refresh_from_db()
    assert site.docker_image_id is None
    assert site.operation.pk == operation.pk
    delay.assert_not_called()


def test_failed_build_can_change_image_but_failed_deletion_cannot(client, student, site, image):
    client.force_login(student)
    operation = site.start_operation("fix_site")
    Action.objects.create(
        operation=operation, slug="build_image", name="Building image", result=False
    )
    with patch.object(images_tasks.rebuild_image, "delay"):
        assert (
            client.post(
                reverse("sites:image-select", args=[site.pk]), {"image": image.pk}
            ).status_code
            == 302
        )
    replacement = Operation.objects.get(site=site)
    replacement.ty = "delete_site"
    replacement.save()
    Action.objects.create(
        operation=replacement, slug="delete_files", name="Deleting files", result=False
    )
    with patch.object(images_tasks.rebuild_image, "delay") as delay:
        assert (
            client.post(reverse("sites:image-select", args=[site.pk]), {"image": ""}).status_code
            == 200
        )
    site.refresh_from_db()
    assert site.docker_image_id == image.pk
    delay.assert_not_called()


def test_missing_template_and_install_prefix_are_validation_errors(student, site, image):
    image.install_command_prefix = ""
    image.run_script_template = ""
    image.save()
    form = ImageSelectForm(
        {"image": image.pk, "packages": "requests", "write_run_sh_file": "on"},
        site=site,
        user=student,
    )
    assert not form.is_valid()
    assert {"packages", "write_run_sh_file"}.issubset(form.errors)


def test_serialization_orders_setup_and_omits_unrequested_overwrite(site, image):
    late = DockerImageSetupCommand.objects.create(name="Late", command="echo late", order=2)
    early = DockerImageSetupCommand.objects.create(name="Early", command="echo early", order=0)
    image.setup_commands.add(late, early)
    image.base_install_command = "echo base"
    image.save()
    site.docker_image = image
    site.image_packages = "requests"
    setup = image_setup_for_site(site)
    assert setup["setup_commands"] == ["echo early", "echo late", "echo base"]
    assert setup["packages"] == ["requests"]
    assert setup["run_script_template"] is None


def test_image_build_payload_places_setup_inside_site_contract(site, image):
    site.docker_image = image
    site.image_packages = "requests==2.32.3"
    site.image_write_run_script = True
    site.save()
    with patch.object(Appserver, "http_request", return_value=Mock(status_code=200)) as request:
        list(actions.build_docker_image(site, [Appserver("mocked-appserver")]))
    payload = request.call_args.kwargs["data"]
    assert "image_setup" not in payload
    assert payload["site"]["image_setup"] == {
        "base_image": image.name,
        "setup_commands": [],
        "packages": ["requests==2.32.3"],
        "install_command_prefix": "pip install",
        "run_script_template": image.run_script_template,
    }


def test_image_task_builds_deploys_and_clears_overwrite_intent(site, image):
    site.docker_image = image
    site.image_write_run_script = True
    site.save()
    operation = site.start_operation("update_docker_image")
    with framework.mock(
        {"path": "/api/docker/image/build", "data": {}},
        {"path": "/api/docker/service/update", "data": {}},
    ):
        images_tasks.rebuild_image(operation.pk)
    site.refresh_from_db()
    assert not site.image_write_run_script
    assert not Operation.objects.filter(site=site).exists()


def test_failed_image_task_keeps_retryable_desired_setup(site, image):
    site.docker_image = image
    site.image_write_run_script = True
    site.save()
    operation = site.start_operation("update_docker_image")
    with framework.mock(
        {
            "path": "/api/docker/image/build",
            "status_code": 500,
            "data": {"detail": {"user_error": True, "explanation": "Package unavailable"}},
        }
    ):
        images_tasks.rebuild_image(operation.pk)
    site.refresh_from_db()
    assert site.image_write_run_script
    assert site.operation.status == "failed"
    assert "Package unavailable" in site.operation.action_set.first().user_message


@pytest.mark.parametrize(
    "data",
    (
        {"cpus": "nan"},
        {"cpus": "inf"},
        {"cpus": "0"},
        {"cpus": "4"},
        {"memory": "0"},
        {"memory": "huge"},
        {"max_request_body_size": "-1"},
    ),
)
def test_resource_limits_validate_finite_positive_supported_values(data):
    assert not SiteResourceLimitsForm(data).is_valid()


def test_only_admin_can_change_resources(client, student, site):
    client.force_login(student)
    url = reverse("sites:resource-edit", args=[site.pk])
    assert client.get(url).status_code == 403
    assert client.post(url, {"cpus": 2}).status_code == 403


def test_resource_limits_persist_queue_deployment_and_blank_restores_defaults(
    client, administrator, site, settings
):
    client.force_login(administrator)
    url = reverse("sites:resource-edit", args=[site.pk])
    with patch.object(images_tasks.apply_resource_limits, "delay") as delay:
        assert (
            client.post(
                url,
                {
                    "cpus": "1.5",
                    "memory": "512 MiB",
                    "max_request_body_size": "10 MB",
                    "notes": "Approved project",
                },
            ).status_code
            == 302
        )
    limits = SiteResourceLimits.objects.get(site=site)
    assert limits.cpus == 1.5
    assert limits.memory == str(512 * 1024**2)
    assert limits.max_request_body_size == 10 * 1000**2
    delay.assert_called_once_with(site.operation.pk)
    with framework.mock({"path": "/api/docker/service/update", "data": {}}):
        images_tasks.apply_resource_limits(site.operation.pk)
    assert not Operation.objects.filter(site=site).exists()
    with patch.object(images_tasks.apply_resource_limits, "delay"):
        assert (
            client.post(
                url, {"cpus": "", "memory": "", "max_request_body_size": "", "notes": ""}
            ).status_code
            == 302
        )
    site.refresh_from_db()
    assert site.serialize_resource_limits() == {
        "cpus": settings.DIRECTOR_RESOURCES_DEFAULT_CPUS,
        "memory": settings.DIRECTOR_RESOURCES_DEFAULT_MEMORY_LIMIT,
        "max_request_body_size": settings.DIRECTOR_RESOURCES_MAX_REQUEST_BODY,
    }


def test_queue_failure_keeps_image_changes_retryable(client, student, site, image):
    client.force_login(student)
    with patch.object(images_tasks.rebuild_image, "delay", side_effect=RuntimeError("offline")):
        assert (
            client.post(
                reverse("sites:image-select", args=[site.pk]), {"image": image.pk}
            ).status_code
            == 302
        )
    site.refresh_from_db()
    assert site.docker_image_id == image.pk
    assert site.operation.status == "failed"
    with patch.object(images_tasks.rebuild_image, "delay") as delay:
        assert client.post(reverse("sites:retry", args=[site.pk]), {}).status_code == 302
    site.refresh_from_db()
    delay.assert_called_once_with(site.operation.pk)


def test_resource_queue_failure_keeps_limits_and_retries_service_deployment(
    client, administrator, site
):
    client.force_login(administrator)
    with patch.object(
        images_tasks.apply_resource_limits, "delay", side_effect=RuntimeError("offline")
    ):
        response = client.post(
            reverse("sites:resource-edit", args=[site.pk]), {"cpus": "2", "memory": "1 GiB"}
        )
    assert response.status_code == 302
    assert SiteResourceLimits.objects.get(site=site).cpus == 2
    assert site.operation.ty == "update_resource_limits"
    assert site.operation.status == "failed"
    with patch.object(images_tasks.apply_resource_limits, "delay") as delay:
        assert client.post(reverse("sites:retry", args=[site.pk]), {}).status_code == 302
    site.refresh_from_db()
    delay.assert_called_once_with(site.operation.pk)
