from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from ..governance import GuidelinesMiddleware, governance_context, guidelines_api
from ..models import Site, SiteRequest


@pytest.fixture
def site_request(student, teacher):
    return SiteRequest.objects.create(user=student, teacher=teacher, activity="Robotics")


@pytest.fixture
def governance_admin(django_user_model):
    return django_user_model.objects.create_user(
        username="governance-admin",
        is_superuser=True,
        is_staff=True,
        accepted_guidelines=True,
    )


def test_student_request_uses_session_identity_and_does_not_provision(
    client, student, teacher, mailoutbox, django_capture_on_commit_callbacks
):
    teacher.email = "teacher@example.com"
    teacher.save(update_fields=["email"])
    client.force_login(student)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("sites:request-create"),
            {
                "activity": "Robotics",
                "extra_information": "Competition resources",
                "teacher": teacher.pk,
                "student_agreement": "on",
                "user": teacher.pk,
                "teacher_approval": "true",
                "admin_approval": "true",
            },
        )
    assert response.status_code == 302
    assert response.url == reverse("sites:request-status")
    item = SiteRequest.objects.get()
    assert item.user == student
    assert item.teacher == teacher
    assert item.teacher_approval is None
    assert item.admin_approval is None
    assert Site.objects.count() == 0
    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == [teacher.email]
    assert "Competition resources" not in mailoutbox[0].body


@pytest.mark.parametrize(
    "override",
    (
        {"student_agreement": ""},
        {"activity": ""},
        {"activity": "x" * 33},
        {"teacher": "not-an-id"},
    ),
)
def test_invalid_student_request_preserves_input(client, student, teacher, override):
    client.force_login(student)
    data = {
        "activity": "Robotics",
        "extra_information": "Keep these details",
        "teacher": teacher.pk,
        "student_agreement": "on",
        **override,
    }
    response = client.post(reverse("sites:request-create"), data)
    assert response.status_code == 400
    assert b"Keep these details" in response.content
    assert b'role="alert"' in response.content
    assert not SiteRequest.objects.exists()


def test_request_rejects_non_teacher_and_inactive_teacher(client, student, teacher):
    client.force_login(student)
    data = {"activity": "Robotics", "student_agreement": "on", "teacher": student.pk}
    assert client.post(reverse("sites:request-create"), data).status_code == 400
    teacher.is_active = False
    teacher.save(update_fields=["is_active"])
    data["teacher"] = teacher.pk
    assert client.post(reverse("sites:request-create"), data).status_code == 400
    assert not SiteRequest.objects.exists()


@pytest.mark.parametrize(
    ("url_name", "user_fixture"),
    (
        ("request-create", "teacher"),
        ("request-status", "teacher"),
        ("request-teacher", "student"),
        ("request-admin", "teacher"),
        ("request-admin", "student"),
    ),
)
def test_governance_role_checks(client, request, url_name, user_fixture):
    client.force_login(request.getfixturevalue(user_fixture))
    assert client.get(reverse(f"sites:{url_name}")).status_code == 403
    if url_name != "request-status":
        assert client.post(reverse(f"sites:{url_name}"), {}).status_code == 403


def test_request_status_is_private_and_escaped(
    client, student, teacher, django_user_model, site_request
):
    other = django_user_model.objects.create_user(
        username="other-student", is_student=True, accepted_guidelines=True
    )
    SiteRequest.objects.create(user=other, teacher=teacher, activity="Private activity")
    site_request.admin_comments = "<script>alert('public')</script>"
    site_request.private_admin_comments = "Internal review notes"
    site_request.save()
    client.force_login(student)
    response = client.get(reverse("sites:request-status"))
    assert b"Robotics" in response.content
    assert b"Private activity" not in response.content
    assert b"Internal review notes" not in response.content
    assert b"&lt;script&gt;" in response.content
    assert b"<script>alert" not in response.content


def test_teacher_approval_requires_agreement(client, teacher, site_request):
    client.force_login(teacher)
    response = client.post(
        reverse("sites:request-teacher"), {"request": site_request.pk, "action": "accept"}
    )
    assert response.status_code == 400
    assert b"Accept the teacher agreement" in response.content
    assert b'aria-invalid="true"' in response.content
    site_request.refresh_from_db()
    assert site_request.teacher_approval is None


def test_teacher_can_reject_without_agreement_and_cannot_reverse_review(
    client, teacher, site_request
):
    client.force_login(teacher)
    url = reverse("sites:request-teacher")
    assert client.post(url, {"request": site_request.pk, "action": "reject"}).status_code == 302
    assert (
        client.post(
            url, {"request": site_request.pk, "action": "accept", "agreement": "on"}
        ).status_code
        == 302
    )
    site_request.refresh_from_db()
    assert site_request.teacher_approval is False


def test_teacher_cannot_review_another_teachers_request(
    client, teacher, django_user_model, site_request
):
    other = django_user_model.objects.create_user(
        username="other-teacher", is_teacher=True, accepted_guidelines=True
    )
    client.force_login(other)
    assert b"Robotics" not in client.get(reverse("sites:request-teacher")).content
    response = client.post(
        reverse("sites:request-teacher"),
        {"request": site_request.pk, "action": "accept", "agreement": "on"},
    )
    assert response.status_code == 404
    site_request.refresh_from_db()
    assert site_request.teacher_approval is None


def test_teacher_agreement_ids_are_distinct(client, student, teacher, site_request):
    second = SiteRequest.objects.create(user=student, teacher=teacher, activity="Debate")
    client.force_login(teacher)
    response = client.get(reverse("sites:request-teacher"))
    for item in [site_request, second]:
        expected = f'id="request-{item.pk}-agreement"'.encode()
        assert response.content.count(expected) == 1


@pytest.mark.parametrize("teacher_approval", (None, False))
def test_admin_requires_teacher_approval(client, governance_admin, site_request, teacher_approval):
    site_request.teacher_approval = teacher_approval
    site_request.save()
    client.force_login(governance_admin)
    response = client.post(
        reverse("sites:request-admin"),
        {"request": site_request.pk, "action": "accept", "admin_comments": "Approved"},
    )
    assert response.status_code == 404
    site_request.refresh_from_db()
    assert site_request.admin_approval is None
    assert site_request.admin_comments == ""


def test_approval_workflow_records_comments_without_provisioning(
    client, governance_admin, site_request, mailoutbox, django_capture_on_commit_callbacks
):
    client.force_login(site_request.teacher)
    with django_capture_on_commit_callbacks(execute=True):
        assert (
            client.post(
                reverse("sites:request-teacher"),
                {"request": site_request.pk, "action": "accept", "agreement": "on"},
            ).status_code
            == 302
        )
    assert len(mailoutbox) == 1
    client.force_login(governance_admin)
    response = client.post(
        reverse("sites:request-admin"),
        {
            "request": site_request.pk,
            "action": "accept",
            "admin_comments": "Public decision",
            "private_admin_comments": "Internal review notes",
        },
    )
    assert response.status_code == 302
    site_request.refresh_from_db()
    assert site_request.teacher_approval is True
    assert site_request.admin_approval is True
    assert site_request.admin_comments == "Public decision"
    assert site_request.private_admin_comments == "Internal review notes"
    admin_page = client.get(reverse("sites:request-admin"))
    assert b"Internal review notes" in admin_page.content
    client.force_login(site_request.teacher)
    assert b"Internal review notes" not in client.get(reverse("sites:request-teacher")).content
    client.force_login(site_request.user)
    student_page = client.get(reverse("sites:request-status"))
    assert b"Public decision" in student_page.content
    assert b"Internal review notes" not in student_page.content
    assert Site.objects.count() == 0
    assert len(mailoutbox) == 1
    assert "Internal review notes" not in mailoutbox[0].body
    assert "Public decision" not in mailoutbox[0].body


def test_admin_can_revisit_rejection_but_processed_request_is_read_only(
    client, governance_admin, site_request
):
    site_request.teacher_approval = True
    site_request.save()
    client.force_login(governance_admin)
    url = reverse("sites:request-admin")
    assert client.post(url, {"request": site_request.pk, "action": "reject"}).status_code == 302
    assert (
        client.post(
            url, {"request": site_request.pk, "action": "accept", "admin_comments": "Processed"}
        ).status_code
        == 302
    )
    client.post(
        url, {"request": site_request.pk, "action": "reject", "admin_comments": "Overwrite"}
    )
    site_request.refresh_from_db()
    assert site_request.admin_approval is True
    assert site_request.admin_comments == "Processed"


def test_unknown_admin_action_does_not_change_comments(client, governance_admin, site_request):
    site_request.teacher_approval = True
    site_request.save()
    client.force_login(governance_admin)
    response = client.post(
        reverse("sites:request-admin"),
        {"request": site_request.pk, "action": "other", "admin_comments": "Overwrite"},
    )
    assert response.status_code == 400
    site_request.refresh_from_db()
    assert site_request.admin_approval is None
    assert site_request.admin_comments == ""


@pytest.mark.parametrize("url_name", ("request-teacher", "request-admin"))
@pytest.mark.parametrize("identifier", ("NaN", "-1", str(2**64)))
def test_malformed_request_identifier_returns_bad_request(client, request, url_name, identifier):
    client.force_login(
        request.getfixturevalue("teacher" if url_name == "request-teacher" else "governance_admin")
    )
    response = client.post(
        reverse(f"sites:{url_name}"), {"request": identifier, "action": "accept"}
    )
    assert response.status_code == 400


def test_governance_posts_require_csrf(student, teacher, governance_admin, site_request):
    csrf_client = Client(enforce_csrf_checks=True)
    for user, url_name in [
        (student, "request-create"),
        (teacher, "request-teacher"),
        (governance_admin, "request-admin"),
    ]:
        csrf_client.force_login(user)
        assert csrf_client.post(reverse(f"sites:{url_name}"), {}).status_code == 403
    assert not SiteRequest.objects.exclude(pk=site_request.pk).exists()
    student.accepted_guidelines = False
    student.save(update_fields=["accepted_guidelines"])
    csrf_client.force_login(student)
    assert csrf_client.post(reverse("sites:guidelines"), {"accepted": "on"}).status_code == 403


def test_guidelines_require_valid_acceptance_and_preserve_safe_return(client, student):
    student.accepted_guidelines = False
    student.save()
    client.force_login(student)
    target = reverse("sites:request-status")
    response = client.get(target)
    assert response.status_code == 302
    redirected = urlsplit(response.url)
    assert redirected.path == reverse("sites:guidelines")
    assert parse_qs(redirected.query)["next"] == [target]
    assert client.get(response.url).status_code == 200
    invalid = client.post(reverse("sites:guidelines"), {"next": target})
    assert invalid.status_code == 400
    assert b'role="alert"' in invalid.content
    student.refresh_from_db()
    assert not student.accepted_guidelines
    accepted = client.post(reverse("sites:guidelines"), {"accepted": "on", "next": target})
    assert accepted.status_code == 302
    assert accepted.url == target
    student.refresh_from_db()
    assert student.accepted_guidelines
    assert client.get(target).status_code == 200


@pytest.mark.parametrize(
    "target", ("https://example.net/", "//example.net/", "javascript:alert(1)")
)
def test_guidelines_reject_unsafe_return_urls(client, student, target):
    student.accepted_guidelines = False
    student.save()
    client.force_login(student)
    response = client.post(reverse("sites:guidelines"), {"accepted": "on", "next": target})
    assert response.status_code == 302
    assert response.url == reverse("sites:index")


def test_guidelines_read_and_auth_remain_reachable(client, student):
    student.accepted_guidelines = False
    student.save()
    client.force_login(student)
    response = client.get(reverse("sites:guidelines-read"))
    assert response.status_code == 200
    assert b"Last Updated: 2016-12-19" in response.content
    assert client.get(reverse("auth:login")).status_code == 302


def test_guidelines_api_and_htmx_gates(student, settings):
    student.accepted_guidelines = False
    factory = RequestFactory()
    middleware = GuidelinesMiddleware(lambda request: HttpResponse())

    @guidelines_api
    def api_view(request):
        return HttpResponse()

    api_request = factory.get("/files/download/")
    api_request.user = student
    api_request.resolver_match = SimpleNamespace(namespace="sites:files")
    response = middleware.process_view(api_request, api_view, (), {})
    assert response.status_code == 401
    assert b"guidelines_url" in response.content

    ui_request = factory.post("/create/", HTTP_HX_REQUEST="true")
    ui_request.user = student
    ui_request.resolver_match = SimpleNamespace(namespace="sites")
    response = middleware.process_view(ui_request, lambda request: HttpResponse(), (), {})
    assert response.status_code == 200
    assert response["HX-Redirect"].startswith(reverse("sites:guidelines"))

    settings.DIRECTOR_REQUIRE_GUIDELINES = False
    assert middleware.process_view(api_request, api_view, (), {}) is None


def test_notices_and_pending_request_counts_are_scoped(
    student, teacher, governance_admin, site_request
):
    factory = RequestFactory()
    request = factory.get("/")
    student.graduation_year = timezone.localdate().year
    request.user = student
    assert governance_context(request)["is_graduating_soon"]
    request.user = teacher
    assert governance_context(request)["pending_teacher_requests"] == 1
    request.user = governance_admin
    assert governance_context(request)["pending_admin_requests"] == 0
    site_request.teacher_approval = True
    site_request.save()
    assert governance_context(request)["pending_admin_requests"] == 1
