"""Verify recipient scope, trusted links, and failure isolation using local mail only."""

from unittest.mock import patch

import pytest
from django.db import transaction
from django.urls import reverse

from ..models import Database, DatabaseHost, Site, SiteRequest
from ..notifications import (
    manager_url,
    notify_site_created,
    notify_site_member_added,
    notify_site_request_submitted,
    notify_site_request_teacher_approved,
)


@pytest.fixture(autouse=True)
def notification_settings(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.DIRECTOR_CONTACT_EMAIL = "director-admin@example.test"
    settings.DIRECTOR_MANAGER_URL = "https://director.example.test/"


@pytest.fixture
def notification_site(student, teacher):
    student.email = "creator@example.test"
    student.save(update_fields=["email"])
    teacher.email = "member@example.test"
    teacher.save(update_fields=["email"])
    site = Site.objects.create(
        name="notification-site",
        mode="dynamic",
        purpose="project",
        description="Do not email this site description",
        admin_comments="Private site administration comments",
    )
    site.users.add(student, teacher)
    host = DatabaseHost.objects.create(
        hostname="private-database.example.test",
        port=5432,
        dbms="postgres",
        admin_username="private-admin-user",
        admin_password="private-admin-secret",
    )
    site.database = Database.objects.create(host=host, password="private-site-secret")
    site.save(update_fields=["database"])
    return site


@pytest.fixture
def notification_request(student, teacher):
    student.email = "requester@example.test"
    student.save(update_fields=["email"])
    teacher.email = "assigned-teacher@example.test"
    teacher.save(update_fields=["email"])
    return SiteRequest.objects.create(
        user=student,
        teacher=teacher,
        activity="Robotics",
        extra_information="Do not email additional request information",
        admin_comments="Comments for reviewers",
        private_admin_comments="Private request administration comments",
    )


def test_new_site_notifications_are_individual_and_exclude_actor_and_inactive_users(
    notification_site, student, teacher, django_user_model, mailoutbox
):
    other = django_user_model.objects.create_user(
        username="another-member",
        email="another-member@example.test",
        is_active=True,
    )
    inactive = django_user_model.objects.create_user(
        username="inactive-member",
        email="inactive-member@example.test",
        is_active=False,
    )
    notification_site.users.add(other, inactive)
    assert notify_site_created(notification_site, student) is True
    assert len(mailoutbox) == 2
    assert {message.to[0] for message in mailoutbox} == {teacher.email, other.email}
    for message in mailoutbox:
        assert len(message.to) == 1
        assert message.cc == []
        assert message.bcc == []
        assert message.reply_to == ["director-admin@example.test"]
        assert (
            "https://director.example.test"
            + reverse("sites:dashboard", args=[notification_site.pk])
            in message.body
        )
        assert notification_site.name in message.body
        assert "private" not in message.body
        assert notification_site.description not in message.body


@pytest.mark.parametrize("excluded", ("actor", "inactive", "service", "removed"))
def test_member_notification_requires_current_eligible_membership(
    notification_site, student, teacher, mailoutbox, excluded
):
    member = teacher
    if excluded == "actor":
        member = student
    elif excluded == "inactive":
        teacher.is_active = False
    elif excluded == "service":
        teacher.is_service = True
    elif excluded == "removed":
        notification_site.users.remove(teacher)
    assert notify_site_member_added(notification_site, member, student) is False
    assert not mailoutbox


def test_submitted_request_only_notifies_assigned_teacher(notification_request, mailoutbox):
    assert notify_site_request_submitted(notification_request) is True
    assert len(mailoutbox) == 1
    message = mailoutbox[0]
    assert message.to == [notification_request.teacher.email]
    assert notification_request.activity in message.body
    assert "https://director.example.test" + reverse("sites:request-teacher") in message.body
    assert notification_request.extra_information not in message.body
    assert notification_request.admin_comments not in message.body
    assert notification_request.private_admin_comments not in message.body


@pytest.mark.parametrize("teacher_approval", (None, False, True))
def test_only_teacher_approved_pending_requests_notify_administrator(
    notification_request, mailoutbox, teacher_approval
):
    notification_request.teacher_approval = teacher_approval
    notification_request.save(update_fields=["teacher_approval"])
    delivered = notify_site_request_teacher_approved(notification_request)
    assert delivered is (teacher_approval is True)
    if teacher_approval is True:
        assert len(mailoutbox) == 1
        message = mailoutbox[0]
        assert message.to == ["director-admin@example.test"]
        assert "https://director.example.test" + reverse("sites:request-admin") in message.body
        assert notification_request.admin_comments not in message.body
        assert notification_request.private_admin_comments not in message.body
    else:
        assert not mailoutbox


@pytest.mark.parametrize("approval", (False, True))
def test_processed_requests_do_not_send_teacher_or_admin_notifications(
    notification_request, mailoutbox, approval
):
    notification_request.teacher_approval = True
    notification_request.admin_approval = approval
    notification_request.save(update_fields=["teacher_approval", "admin_approval"])
    assert notify_site_request_submitted(notification_request) is False
    assert notify_site_request_teacher_approved(notification_request) is False
    assert not mailoutbox


def test_unsaved_request_is_not_emailed(student, teacher, mailoutbox):
    item = SiteRequest(user=student, teacher=teacher, activity="Unsaved")
    assert notify_site_request_submitted(item) is False
    item.teacher_approval = True
    assert notify_site_request_teacher_approved(item) is False
    assert not mailoutbox


@pytest.mark.parametrize(
    "email", ("", "not-an-address", "valid@example.test\nBcc: other@example.test")
)
def test_invalid_recipient_never_reaches_mail_backend(
    notification_site, teacher, mailoutbox, caplog, email
):
    student = notification_site.users.get(username="student")
    teacher.email = email
    with patch("director.apps.sites.notifications.EmailMessage.send") as send:
        assert notify_site_member_added(notification_site, teacher, student) is False
    send.assert_not_called()
    assert not mailoutbox
    assert email not in caplog.text or not email


@pytest.mark.parametrize(
    "origin",
    (
        "//untrusted.example.test",
        "javascript:alert(1)",
        "https://user:secret@director.example.test",
        "https://director.example.test/?next=external",
        "https://director.example.test/#fragment",
        "https://director.example.test:bad",
        "https://director.example.test\\untrusted",
        "https://director.example.test bad",
    ),
)
def test_invalid_manager_origin_cannot_appear_in_notifications(
    settings, notification_site, mailoutbox, caplog, origin
):
    student = notification_site.users.get(username="student")
    teacher = notification_site.users.get(username="teacher")
    settings.DIRECTOR_MANAGER_URL = origin
    assert notify_site_member_added(notification_site, teacher, student) is False
    assert not mailoutbox
    assert origin not in caplog.text


def test_manager_links_preserve_configured_path_prefix(settings):
    settings.DIRECTOR_MANAGER_URL = "https://director.example.test/manager"
    assert manager_url("sites:request-teacher") == (
        "https://director.example.test/manager" + reverse("sites:request-teacher")
    )


@pytest.mark.parametrize("send_result", (0, OSError("SMTP password=private-mail-secret")))
def test_failed_delivery_keeps_persisted_state_and_logs_no_private_data(
    notification_request, caplog, send_result
):
    item = notification_request
    item.teacher_approval = True
    item.save(update_fields=["teacher_approval"])
    kwargs = (
        {"side_effect": send_result}
        if isinstance(send_result, Exception)
        else {"return_value": send_result}
    )
    with patch("director.apps.sites.notifications.EmailMessage.send", **kwargs):
        assert notify_site_request_teacher_approved(item) is False
    item.refresh_from_db()
    assert item.teacher_approval is True
    assert "notification" in caplog.text
    assert "private-mail-secret" not in caplog.text
    assert item.private_admin_comments not in caplog.text
    assert item.teacher.email not in caplog.text


def test_commit_callback_waits_for_persistence_and_rollback_sends_nothing(
    notification_site, student, teacher, mailoutbox, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        transaction.on_commit(lambda: notify_site_member_added(notification_site, teacher, student))
        assert not mailoutbox
    assert len(callbacks) == 1
    assert len(mailoutbox) == 1
    mailoutbox.clear()

    def abandoned_transaction():
        with transaction.atomic():
            transaction.on_commit(
                lambda: notify_site_member_added(notification_site, teacher, student)
            )
            raise RuntimeError("abort membership")

    with django_capture_on_commit_callbacks(execute=True) as rolled_back_callbacks:
        with pytest.raises(RuntimeError, match="abort membership"):
            abandoned_transaction()
    assert not rolled_back_callbacks
    assert not mailoutbox
