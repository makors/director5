"""Director4 creation policy and structured, permission-scoped search."""

import re
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.urls import reverse

from ..models import Site


@pytest.fixture(autouse=True)
def memory_channels(settings):
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


def create_data(**overrides):
    return {
        "name": "new-project",
        "mode": "static",
        "purpose": "project",
        "student_agreement": True,
        **overrides,
    }


def test_creation_optional_fields_start_collapsed_and_agreement_stays_visible(client, student):
    client.force_login(student)
    html = client.get(reverse("sites:create")).content.decode()
    optional = re.search(r'<details[^>]*id="create-additional-options"[^>]*>', html).group()
    assert "open" not in optional
    assert html.index("</details>") < html.index('name="student_agreement"')


@pytest.mark.parametrize("htmx", (False, True))
@pytest.mark.parametrize("optional", ({"users": ["unknown"]}, {"description": "Keep this text"}))
def test_creation_reveals_optional_values_and_errors(client, student, htmx, optional):
    client.force_login(student)
    response = client.post(
        reverse("sites:create"),
        create_data(name="UPPERCASE", **optional),
        headers={"HX-Request": "true"} if htmx else {},
    )
    html = response.content.decode()
    tag = re.search(r'<details[^>]*id="create-additional-options"[^>]*>', html).group()
    assert "open" in tag
    if "description" in optional:
        assert optional["description"] in html
    else:
        assert response.context["form"].errors["users"]
    assert not Site.objects.exists()


@pytest.mark.parametrize("agreement", (False, ""))
def test_creation_requires_agreement(client, student, agreement):
    client.force_login(student)
    with patch("director.apps.sites.tasks.create_site.delay") as delay:
        response = client.post(reverse("sites:create"), create_data(student_agreement=agreement))
    assert response.context["form"].errors["student_agreement"]
    assert not Site.objects.exists()
    delay.assert_not_called()


def test_creator_is_member_and_purpose_cannot_be_forged(client, student, teacher):
    client.force_login(student)
    with (
        patch("director.apps.sites.tasks.create_site.delay"),
        patch("director.apps.sites.views.notify_site_created") as notify,
        TestCase.captureOnCommitCallbacks(execute=True),
    ):
        response = client.post(
            reverse("sites:create"), create_data(purpose="activity", users=[teacher.pk])
        )
    assert response.status_code == 302
    site = Site.objects.get()
    assert site.purpose == "project"
    assert set(site.users.values_list("pk", flat=True)) == {student.pk, teacher.pk}
    notify.assert_called_once_with(site, student)


@pytest.mark.parametrize("superuser", (False, True))
def test_personal_site_locks_identity_and_membership(client, student, teacher, superuser):
    student.is_superuser = superuser
    student.save(update_fields=["is_superuser"])
    client.force_login(student)
    with patch("director.apps.sites.tasks.create_site.delay") as delay:
        response = client.post(
            reverse("sites:create-personal"),
            create_data(name="forged", purpose="activity", users=[teacher.pk]),
        )
    site = Site.objects.get()
    assert site.name == student.username
    assert site.purpose == "user"
    assert list(site.users.all()) == [student]
    assert response.url == reverse("sites:dashboard", args=[site.pk])
    delay.assert_called_once()
    assert client.get(reverse("sites:create-personal")).url == response.url
    assert Site.objects.count() == 1


@pytest.mark.parametrize("pattern", ("^restricted", "["))
def test_reserved_or_misconfigured_names_fail_closed(client, student, settings, pattern):
    settings.FORBIDDEN_SITE_NAME_REGEX = pattern
    client.force_login(student)
    with patch("director.apps.sites.tasks.create_site.delay") as delay:
        response = client.post(reverse("sites:create"), create_data(name="restricted-project"))
    assert response.context["form"].errors["name"]
    assert not Site.objects.exists()
    delay.assert_not_called()


def test_structured_search_is_and_scoped_and_unique(client, student, teacher):
    selected = Site.objects.create(
        name="physics-lab", description="Quantum optics", mode="dynamic", purpose="project"
    )
    selected.users.add(student, teacher)
    Site.objects.create(
        name="other-lab", description="Quantum optics", mode="dynamic", purpose="project"
    ).users.add(student)
    Site.objects.create(
        name="private-lab", description="Quantum optics", mode="dynamic", purpose="project"
    ).users.add(teacher)
    client.force_login(student)
    response = client.get(
        reverse("sites:index"),
        {"q": 'name:lab desc:"Quantum optics" user:teacher', "mode": "dynamic"},
    )
    assert list(response.context["sites"]) == [selected]
    assert response.context["result_count"] == 1
    assert list(
        client.get(reverse("sites:index"), {"q": f"id:{selected.pk}"}).context["sites"]
    ) == [selected]
    assert not client.get(reverse("sites:index"), {"q": "id:" + "9" * 100}).context["sites"]


def test_listing_paginates_and_admin_can_find_unowned_sites(client, student, teacher):
    student.is_superuser = True
    student.save(update_fields=["is_superuser"])
    for index in range(32):
        Site.objects.create(
            name=f"project-{index:02d}", mode="static", purpose="project"
        ).users.add(student)
    other = Site.objects.create(name="other-person", mode="static", purpose="project")
    other.users.add(teacher)
    client.force_login(student)
    response = client.get(reverse("sites:index"))
    assert response.context["result_count"] == 32
    assert len(response.context["sites"]) == 30
    assert len(client.get(reverse("sites:index"), {"page": 2}).context["sites"]) == 2
    assert client.get(reverse("sites:index"), {"all": "1"}).context["result_count"] == 33
    assert list(
        client.get(reverse("sites:index"), {"q": "name:other-person"}).context["sites"]
    ) == [other]
