import uuid

import pytest
from director.apps.users.models import MassEmail
from django.test import Client
from django.urls import reverse

from .. import docs_views, mail_views


@pytest.fixture(autouse=True)
def memory_email_backend(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def docs_root(settings, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "index.md").write_text(
        "# Director handbook\n\n[Installation](guide/install.md)\n\n"
        "```{toctree}\nguide/install\n```\n",
        encoding="utf-8",
    )
    (root / "guide").mkdir()
    (root / "guide" / "install.md").write_text(
        "# Installation\n\nDeploy a website.\n\n[Home](../index.md)\n", encoding="utf-8"
    )
    settings.DIRECTOR_DOCS_ROOT = root
    return root


@pytest.fixture
def mail_admin(django_user_model):
    return django_user_model.objects.create_user(
        username="mail-admin",
        email="admin@example.com",
        is_superuser=True,
        is_staff=True,
        accepted_guidelines=True,
    )


@pytest.fixture
def mail_recipient(django_user_model):
    return django_user_model.objects.create_user(
        username="recipient", email="recipient@example.com", accepted_guidelines=True
    )


@pytest.fixture
def mail_draft(mail_admin, mail_recipient):
    draft = MassEmail.objects.create(
        sender=mail_admin,
        subject="Maintenance",
        text_plain="Maintenance notice",
        text_html="<p>Maintenance notice</p>",
    )
    draft.limit_users.add(mail_recipient)
    return draft


def _compose_data(recipient=None, request_id=None):
    data = {
        "request_id": str(request_id or uuid.uuid4()),
        "subject": "Maintenance",
        "text_plain": "Maintenance notice",
        "text_html": "<p>Maintenance notice</p>",
    }
    if recipient:
        data["limit_users"] = [recipient.pk]
    return data


def _confirm_data(draft):
    return {"request_id": str(draft.request_id), "confirm_send": "on"}


def test_docs_read_links_and_search(client, student, docs_root):
    client.force_login(student)
    index = client.get(reverse("sites:docs-index"))
    assert index.status_code == 200
    assert b"Director handbook" in index.content
    installation_url = reverse("sites:docs-page", args=["guide/install"])
    assert installation_url.encode() in index.content
    assert b"{toctree}" not in index.content
    page = client.get(installation_url)
    assert page.status_code == 200
    assert b"Deploy a website" in page.content
    assert reverse("sites:docs-index").encode() in page.content
    search = client.get(reverse("sites:docs-search"), {"q": "installation"})
    assert search.status_code == 200
    assert search.context["results"][0]["title"] == "Installation"
    assert client.post(reverse("sites:docs-search"), {"q": "installation"}).status_code == 405


def test_docs_need_login_but_remain_readable_before_guidelines(client, student, docs_root):
    assert client.get(reverse("sites:docs-index")).status_code == 302
    student.accepted_guidelines = False
    student.save()
    client.force_login(student)
    assert client.get(reverse("sites:docs-index")).status_code == 200


@pytest.mark.parametrize(
    "page",
    (
        "../private",
        "/private",
        "guide/../../private",
        "conf.py",
        "_private",
        ".secret",
        "guide\\private",
    ),
)
def test_docs_reject_private_paths(client, student, docs_root, page):
    (docs_root.parent / "private.md").write_text("Private credentials", encoding="utf-8")
    client.force_login(student)
    assert client.get(reverse("sites:docs-page", args=[page])).status_code == 404


def test_docs_do_not_follow_external_symlinks(client, student, docs_root):
    private = docs_root.parent / "private.md"
    private.write_text("# Private credentials", encoding="utf-8")
    (docs_root / "leak.md").symlink_to(private)
    client.force_login(student)
    assert client.get(reverse("sites:docs-page", args=["leak"])).status_code == 404
    search = client.get(reverse("sites:docs-search"), {"q": "Private"})
    assert b"Private credentials" not in search.content


def test_docs_disable_html_and_unsafe_link_protocols(client, student, docs_root):
    (docs_root / "unsafe.md").write_text(
        "# Unsafe markup\n\n<script>alert('doc-source')</script>\n\n"
        "[Link](javascript:alert(1))\n\n<img src=x onerror=alert(1)>\n",
        encoding="utf-8",
    )
    client.force_login(student)
    response = client.get(reverse("sites:docs-page", args=["unsafe"]))
    assert response.status_code == 200
    assert b"&lt;script&gt;" in response.content
    assert b"<script>alert('doc-source')" not in response.content
    assert b'href="javascript:' not in response.content
    assert b"<img src=x" not in response.content


def test_docs_search_is_bounded(client, student, docs_root, monkeypatch):
    client.force_login(student)
    assert client.get(reverse("sites:docs-search"), {"q": "x" * 201}).status_code == 400
    (docs_root / "large.md").write_text("x" * (docs_views.MAX_DOCUMENT_BYTES + 1), encoding="utf-8")
    assert client.get(reverse("sites:docs-page", args=["large"])).status_code == 404
    monkeypatch.setattr(docs_views, "MAX_DOCUMENTS", 2)
    for number in range(5):
        (docs_root / f"page-{number}.md").write_text(
            f"# Page {number}\n\nShared term", encoding="utf-8"
        )
    assert len(list(docs_views._documents())) <= 2


def test_docs_canonical_markdown_links_and_missing_page(client, student, docs_root):
    client.force_login(student)
    response = client.get(reverse("sites:docs-page", args=["guide/install.md"]))
    assert response.status_code == 302
    assert response.url == reverse("sites:docs-page", args=["guide/install"])
    assert client.get(reverse("sites:docs-page", args=["missing"])).status_code == 404


@pytest.mark.parametrize("url_name", ("mail-compose", "mail-history"))
def test_mass_email_is_superuser_only(client, student, url_name):
    client.force_login(student)
    assert client.get(reverse(f"sites:{url_name}")).status_code == 403
    if url_name == "mail-compose":
        assert client.post(reverse(f"sites:{url_name}"), _compose_data()).status_code == 403
    student.is_staff = True
    student.save()
    assert client.get(reverse(f"sites:{url_name}")).status_code == 403


def test_compose_only_saves_a_reviewable_draft(client, mail_admin, mail_recipient, mailoutbox):
    client.force_login(mail_admin)
    data = _compose_data(mail_recipient)
    data["text_html"] = "<script>alert('mail-preview')</script>"
    response = client.post(reverse("sites:mail-compose"), data)
    assert response.status_code == 302
    draft = MassEmail.objects.get()
    assert draft.status == "queued"
    assert draft.sent_time is None
    assert draft.sender == mail_admin
    assert list(draft.limit_users.all()) == [mail_recipient]
    preview = client.get(response.url)
    assert preview.status_code == 200
    assert b"&lt;script&gt;" in preview.content
    assert b"<script>alert('mail-preview')" not in preview.content
    assert not mailoutbox


@pytest.mark.parametrize(
    "override",
    (
        {"request_id": "not-a-uuid"},
        {"subject": "Hello\nBcc: extra@example.com"},
        {"text_plain": ""},
        {"text_html": ""},
    ),
)
def test_invalid_compose_sends_nothing(client, mail_admin, mailoutbox, override):
    client.force_login(mail_admin)
    response = client.post(reverse("sites:mail-compose"), {**_compose_data(), **override})
    assert response.status_code == 400
    assert b'role="alert"' in response.content
    assert not MassEmail.objects.exists()
    assert not mailoutbox


def test_duplicate_compose_uuid_keeps_the_original_draft(client, mail_admin, mail_recipient):
    client.force_login(mail_admin)
    data = _compose_data(mail_recipient)
    first = client.post(reverse("sites:mail-compose"), data)
    data["subject"] = "Attempted overwrite"
    second = client.post(reverse("sites:mail-compose"), data)
    assert first.status_code == second.status_code == 302
    assert first.url == second.url
    assert MassEmail.objects.count() == 1
    assert MassEmail.objects.get().subject == "Maintenance"


def test_confirmation_requires_checkbox_and_matching_uuid(client, mail_draft, mailoutbox):
    client.force_login(mail_draft.sender)
    url = reverse("sites:mail-detail", args=[mail_draft.request_id])
    assert client.post(url, {"request_id": str(mail_draft.request_id)}).status_code == 400
    data = _confirm_data(mail_draft)
    data["request_id"] = str(uuid.uuid4())
    assert client.post(url, data).status_code == 400
    mail_draft.refresh_from_db()
    assert mail_draft.sent_time is None
    assert mail_draft.status == "queued"
    assert not mailoutbox


def test_confirm_send_uses_bcc_and_is_idempotent(client, mail_draft, mailoutbox):
    client.force_login(mail_draft.sender)
    url = reverse("sites:mail-detail", args=[mail_draft.request_id])
    assert client.get(url).status_code == 200
    assert not mailoutbox
    assert client.post(url, _confirm_data(mail_draft)).status_code == 302
    mail_draft.refresh_from_db()
    assert mail_draft.status == "sent"
    assert mail_draft.sent_time is not None
    assert mail_draft.recipient_emails == ["recipient@example.com"]
    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == []
    assert mailoutbox[0].bcc == ["recipient@example.com"]
    assert mailoutbox[0].alternatives[0].mimetype == "text/html"
    assert client.post(url, _confirm_data(mail_draft)).status_code == 302
    assert len(mailoutbox) == 1


def test_all_user_send_freezes_valid_deduplicated_recipients(
    client, mail_draft, mailoutbox, django_user_model
):
    mail_draft.limit_users.clear()
    django_user_model.objects.create_user(username="duplicate", email="recipient@example.com")
    django_user_model.objects.create_user(username="invalid", email="invalid-address")
    client.force_login(mail_draft.sender)
    url = reverse("sites:mail-detail", args=[mail_draft.request_id])
    client.post(url, _confirm_data(mail_draft))
    mail_draft.refresh_from_db()
    assert mail_draft.recipient_emails == ["admin@example.com", "recipient@example.com"]
    assert mailoutbox[0].bcc == mail_draft.recipient_emails
    recipient = django_user_model.objects.get(username="recipient")
    recipient.email = "changed@example.com"
    recipient.save()
    django_user_model.objects.create_user(username="new-user", email="new@example.com")
    page = client.get(url)
    assert page.context["recipient_count"] == 2
    assert mail_draft.recipient_emails == ["admin@example.com", "recipient@example.com"]


def test_failed_delivery_preserves_history_and_redacts_exception(
    client, mail_draft, monkeypatch, caplog, mailoutbox
):
    def fail(*args):
        raise RuntimeError("smtp://private-example:credential@example.com")

    monkeypatch.setattr(mail_views, "_deliver_message", fail)
    client.force_login(mail_draft.sender)
    url = reverse("sites:mail-detail", args=[mail_draft.request_id])
    response = client.post(url, _confirm_data(mail_draft), follow=True)
    assert response.status_code == 200
    mail_draft.refresh_from_db()
    assert mail_draft.status == "failed"
    assert mail_draft.sent_time is None
    assert mail_draft.recipient_emails == ["recipient@example.com"]
    assert b"Retrying may send duplicate emails" in response.content
    assert "credential@example.com" not in caplog.text
    assert b"credential@example.com" not in response.content
    assert not mailoutbox


def test_zero_backend_result_is_not_marked_sent(client, mail_draft, monkeypatch):
    monkeypatch.setattr(mail_views.EmailMultiAlternatives, "send", lambda *args, **kwargs: 0)
    client.force_login(mail_draft.sender)
    client.post(
        reverse("sites:mail-detail", args=[mail_draft.request_id]), _confirm_data(mail_draft)
    )
    mail_draft.refresh_from_db()
    assert mail_draft.status == "failed"
    assert mail_draft.sent_time is None


def test_failed_retry_keeps_the_attempted_recipient_snapshot(client, mail_draft, mailoutbox):
    mail_draft.status = "failed"
    mail_draft.recipient_emails = ["original@example.com"]
    mail_draft.save()
    client.force_login(mail_draft.sender)
    client.post(
        reverse("sites:mail-detail", args=[mail_draft.request_id]), _confirm_data(mail_draft)
    )
    mail_draft.refresh_from_db()
    assert mail_draft.status == "sent"
    assert mailoutbox[0].bcc == ["original@example.com"]


def test_no_valid_recipients_cannot_be_marked_sent(client, mail_draft, mail_recipient, mailoutbox):
    mail_recipient.email = ""
    mail_recipient.save()
    client.force_login(mail_draft.sender)
    url = reverse("sites:mail-detail", args=[mail_draft.request_id])
    assert b"No recipients have a valid email address" in client.get(url).content
    client.post(url, _confirm_data(mail_draft))
    mail_draft.refresh_from_db()
    assert mail_draft.status == "failed"
    assert mail_draft.sent_time is None
    assert mail_draft.recipient_emails == []
    mail_recipient.email = "new-address@example.com"
    mail_recipient.save()
    client.post(url, _confirm_data(mail_draft))
    mail_draft.refresh_from_db()
    assert mail_draft.status == "failed"
    assert mail_draft.recipient_emails == []
    assert not mailoutbox


def test_other_admin_can_read_history_but_cannot_send_draft(client, mail_draft, django_user_model):
    other = django_user_model.objects.create_user(
        username="other-admin", is_superuser=True, accepted_guidelines=True
    )
    client.force_login(other)
    url = reverse("sites:mail-detail", args=[mail_draft.request_id])
    assert client.get(url).status_code == 200
    assert client.post(url, _confirm_data(mail_draft)).status_code == 403
    data = _compose_data(request_id=mail_draft.request_id)
    assert client.post(reverse("sites:mail-compose"), data).status_code == 403
    mail_draft.refresh_from_db()
    assert mail_draft.status == "queued"


def test_mass_email_posts_require_csrf(mail_draft):
    client = Client(enforce_csrf_checks=True)
    client.force_login(mail_draft.sender)
    assert client.post(reverse("sites:mail-compose"), _compose_data()).status_code == 403
    url = reverse("sites:mail-detail", args=[mail_draft.request_id])
    assert client.post(url, _confirm_data(mail_draft)).status_code == 403
    mail_draft.refresh_from_db()
    assert mail_draft.status == "queued"


def test_email_history_is_paginated_and_preserves_sender_status(client, mail_admin):
    MassEmail.objects.bulk_create(
        [
            MassEmail(
                sender=mail_admin,
                subject=f"Notice {number}",
                text_plain="Text",
                text_html="<p>Text</p>",
            )
            for number in range(31)
        ]
    )
    client.force_login(mail_admin)
    response = client.get(reverse("sites:mail-history"))
    assert response.status_code == 200
    assert len(response.context["page"]) == 30
    assert b"mail-admin" in response.content
    assert b"Awaiting confirmation" in response.content
    second = client.get(reverse("sites:mail-history"), {"page": 2})
    assert len(second.context["page"]) == 1
