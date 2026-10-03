import base64
import json
from unittest.mock import Mock, patch

import pytest
import requests
import responses
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from ..appserver import Appserver
from ..file_views import DownloadStream
from ..models import Site


@pytest.fixture
def file_site(student):
    student.accepted_guidelines = True
    student.save(update_fields=["accepted_guidelines"])
    site = Site.objects.create(name="file-site", mode="static", purpose="project")
    site.users.add(student)
    return site


def endpoint(name, site):
    return reverse(f"sites:files:{name}", args=[site.pk])


def response(data, status=200):
    result = Mock(spec=requests.Response)
    result.status_code = status
    result.json.return_value = data
    return result


def test_editor_renders_safe_configuration_and_controls(client, student, file_site):
    client.force_login(student)
    result = client.get(endpoint("browser", file_site))
    assert result.status_code == 200
    html = result.content.decode()
    assert 'id="file-content"' in html
    assert 'id="file-upload-input"' in html
    assert "file-endpoints" in html
    assert "csrfmiddlewaretoken" in html
    assert endpoint("write", file_site) in html


@pytest.mark.parametrize("name", ("browser", "list", "read", "download", "archive"))
def test_files_require_site_membership(client, student, teacher, file_site, name):
    file_site.users.remove(student)
    file_site.users.add(teacher)
    client.force_login(student)
    with patch.object(Appserver, "http_request") as upstream:
        assert client.get(endpoint(name, file_site)).status_code == 404
    upstream.assert_not_called()


@pytest.mark.parametrize("name", ("write", "upload", "mkdir", "delete", "move", "chmod"))
def test_mutations_require_membership_and_post(client, student, file_site, name):
    client.force_login(student)
    assert client.get(endpoint(name, file_site)).status_code == 405
    file_site.users.remove(student)
    with patch.object(Appserver, "http_request") as upstream:
        assert (
            client.post(endpoint(name, file_site), {"path": "public/index.html"}).status_code == 404
        )
    upstream.assert_not_called()


def test_disabled_site_cannot_be_edited_by_regular_member(client, student, file_site):
    file_site.availability = "disabled"
    file_site.save(update_fields=["availability"])
    client.force_login(student)
    assert client.get(endpoint("browser", file_site)).status_code == 404


def test_editor_save_requires_csrf(client, student, file_site):
    secure_client = Client(enforce_csrf_checks=True)
    secure_client.force_login(student)
    secure_client.get(endpoint("browser", file_site))
    with patch.object(
        Appserver, "http_request", return_value=response({"sha256": "a" * 64})
    ) as upstream:
        denied = secure_client.post(
            endpoint("write", file_site), {"path": "index.html", "content": "Hello"}
        )
        assert denied.status_code == 403
        upstream.assert_not_called()
        token = secure_client.cookies["csrftoken"].value
        allowed = secure_client.post(
            endpoint("write", file_site),
            {"path": "index.html", "content": "Hello"},
            HTTP_X_CSRFTOKEN=token,
        )
        assert allowed.status_code == 200


def test_traversal_rejected_before_contacting_server(client, student, file_site):
    client.force_login(student)
    with patch.object(Appserver, "http_request") as upstream:
        assert client.get(endpoint("read", file_site), {"path": "../secret"}).status_code == 400
        assert (
            client.post(
                endpoint("move", file_site), {"path": "public", "destination": "/outside"}
            ).status_code
            == 400
        )
    upstream.assert_not_called()


def test_ordered_primary_read_failover(client, student, file_site, settings):
    settings.DIRECTOR_APPSERVER_HOSTS = ["primary", "secondary"]
    client.force_login(student)
    called = []

    def request(server, path, method, data):
        called.append(server.host)
        if server.host == "primary":
            raise requests.ConnectionError("offline")
        return response({"entries": [{"name": "index.html", "path": "index.html"}], "path": ""})

    with patch.object(Appserver, "http_request", request):
        result = client.get(endpoint("list", file_site))
    assert result.status_code == 200
    assert called == ["primary", "secondary"]


def test_shared_storage_mutation_runs_once(client, student, file_site, settings):
    settings.DIRECTOR_APPSERVER_HOSTS = ["primary", "secondary"]
    settings.DIRECTOR_SITE_STORAGE_SHARED = True
    client.force_login(student)
    with patch.object(
        Appserver, "http_request", return_value=response({"path": "folder"})
    ) as upstream:
        result = client.post(endpoint("mkdir", file_site), {"path": "folder"})
    assert result.status_code == 200
    assert upstream.call_count == 1


def test_independent_storage_mutation_fans_out(client, student, file_site, settings):
    settings.DIRECTOR_APPSERVER_HOSTS = ["primary", "secondary"]
    settings.DIRECTOR_SITE_STORAGE_SHARED = False
    client.force_login(student)
    called = []

    def request(server, path, method, data):
        called.append(server.host)
        assert data["site"]["pk"] == file_site.pk
        return response({"path": "index.html", "sha256": "a" * 64})

    with patch.object(Appserver, "http_request", request):
        result = client.post(
            endpoint("write", file_site), {"path": "index.html", "content": "Hello"}
        )
    assert result.status_code == 200
    assert called == ["primary", "secondary"]


def test_partial_replication_is_reported_as_failure(client, student, file_site, settings):
    settings.DIRECTOR_APPSERVER_HOSTS = ["primary", "secondary"]
    settings.DIRECTOR_SITE_STORAGE_SHARED = False
    client.force_login(student)
    with patch.object(
        Appserver,
        "http_request",
        side_effect=[response({"path": "index.html"}), requests.ConnectionError("offline")],
    ):
        result = client.post(
            endpoint("write", file_site), {"path": "index.html", "content": "Hello"}
        )
    assert result.status_code == 503
    assert "some servers" in result.json()["error"]


def test_editor_conflict_is_preserved(client, student, file_site):
    client.force_login(student)
    with patch.object(
        Appserver,
        "http_request",
        return_value=response({"detail": "The file changed. Reload before saving."}, 409),
    ):
        result = client.post(
            endpoint("write", file_site),
            {"path": "index.html", "content": "Hello", "expected_sha256": "a" * 64},
        )
    assert result.status_code == 409
    assert "Reload" in result.json()["error"]


def test_binary_upload_sends_bytes_and_server_auth(client, student, file_site, settings):
    settings.DIRECTOR_APPSERVER_TOKEN = "test-files-token"
    client.force_login(student)
    content = b"\x00\xffbinary file"
    with responses.RequestsMock() as mock:
        mock.add(
            "POST",
            "http://mocked-appserver/api/files/upload",
            json={"path": "image.bin"},
            status=200,
        )
        result = client.post(
            endpoint("upload", file_site), {"files": SimpleUploadedFile("image.bin", content)}
        )
        assert result.status_code == 200
        payload = json.loads(mock.calls[0].request.body)
        assert base64.b64decode(payload["content_base64"]) == content
        assert mock.calls[0].request.headers["Authorization"] == "Bearer test-files-token"
        assert payload["create_only"] is True


@pytest.mark.parametrize("name", ("download", "archive"))
def test_binary_download_streams_and_uses_attachment_headers(client, student, file_site, name):
    client.force_login(student)
    with responses.RequestsMock() as mock:
        mock.add(
            "POST", f"http://mocked-appserver/api/files/{name}", body=b"\x00\xffpayload", status=200
        )
        result = client.get(endpoint(name, file_site), {"path": "public/image.bin"})
        assert result.status_code == 200
        assert b"".join(result.streaming_content) == b"\x00\xffpayload"
        assert result["Content-Disposition"].startswith("attachment;")
        assert result["X-Content-Type-Options"] == "nosniff"
        result.close()


def test_download_stream_closes_even_without_consumption():
    upstream = Mock(spec=requests.Response)
    stream = DownloadStream(upstream)
    stream.close()
    upstream.close.assert_called_once()
