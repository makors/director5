"""Authenticated file editor and storage transport for Director sites."""

import base64
from collections.abc import Iterator

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST

from .appserver import Appserver
from .file_forms import (
    MAX_UPLOAD_BYTES,
    ChmodFileForm,
    DeleteFileForm,
    MoveFileForm,
    PathForm,
    WriteFileForm,
    clean_site_path,
)
from .models import Site


class FileBackendError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def file_api(view):
    view.director_guidelines_api = True
    return view


def _site(request, site_id):
    return get_object_or_404(Site.objects.filter_editable(request.user), pk=site_id)


def _servers():
    servers = [Appserver(host) for host in settings.DIRECTOR_APPSERVER_HOSTS]
    if not servers:
        raise FileBackendError("The file service is unavailable. Try again later.")
    return servers


def _response_error(response):
    if response.status_code < 400:
        return
    message = "The file service could not complete this operation."
    try:
        detail = response.json().get("detail")
        if isinstance(detail, str) and response.status_code < 500:
            message = detail[:500]
    except (ValueError, AttributeError):
        pass
    status = response.status_code if response.status_code < 500 else 503
    raise FileBackendError(message, status)


def _replicate(servers, action, payload):
    result = None
    completed = 0
    for server in servers:
        try:
            replica_payload = payload
            if completed and action in {"write", "upload"}:
                # The primary already checked the user's digest/create intent.
                replica_payload = {
                    key: value for key, value in payload.items() if key != "expected_sha256"
                }
                replica_payload["create_only"] = False
            response = server.http_request(f"/api/files/{action}", "POST", replica_payload)
            _response_error(response)
            result = response.json()
            completed += 1
        except (requests.RequestException, ValueError, FileBackendError) as exc:
            message = (
                "The change reached some servers but could not be synchronized everywhere. Retry after the file service recovers."
                if completed
                else "The file service could not apply this change. Try again later."
            )
            if isinstance(exc, FileBackendError) and not completed:
                raise
            raise FileBackendError(message) from exc
    return result


def _request(site, action, data, *, mutation=False):
    payload = {"site": site.serialize_for_appserver()} | data
    servers = _servers()
    shared = getattr(settings, "DIRECTOR_SITE_STORAGE_SHARED", True)
    if mutation and not shared:
        return _replicate(servers, action, payload)
    for server in servers:
        try:
            response = server.http_request(f"/api/files/{action}", "POST", payload)
            _response_error(response)
            return response.json()
        except FileBackendError as exc:
            if exc.status < 500:
                raise
        except (requests.RequestException, ValueError):
            if mutation:
                # A timed-out mutation may already have happened; do not repeat it silently.
                raise FileBackendError(
                    "The file service could not confirm this change. Reload before trying again."
                ) from None
    raise FileBackendError("The file service is unavailable. Try again later.")


def _operation(request, site_id, action, form_class=PathForm, *, mutation=False):
    site = _site(request, site_id)
    form = form_class(request.POST if mutation else request.GET)
    if not form.is_valid():
        return JsonResponse(
            {"error": "Check the file path and values.", "fields": form.errors}, status=400
        )
    data = {
        key: value
        for key, value in form.cleaned_data.items()
        if value != "" or key in {"path", "content"}
    }
    try:
        return JsonResponse(_request(site, action, data, mutation=mutation))
    except FileBackendError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)


@login_required
@require_GET
def browser(request, site_id):
    site = _site(request, site_id)
    names = (
        "list",
        "read",
        "write",
        "upload",
        "mkdir",
        "delete",
        "move",
        "chmod",
        "download",
        "archive",
    )
    return render(
        request,
        "files/browser.html",
        {
            "site": site,
            "can_edit": True,
            "file_endpoints": {
                name: reverse(f"sites:files:{name}", args=[site.pk]) for name in names
            },
        },
    )


@file_api
@login_required
@require_GET
def list_files(request, site_id):
    return _operation(request, site_id, "list")


@file_api
@login_required
@require_GET
def read_file(request, site_id):
    return _operation(request, site_id, "read")


@file_api
@login_required
@require_POST
def write_file(request, site_id):
    return _operation(request, site_id, "write", WriteFileForm, mutation=True)


@file_api
@login_required
@require_POST
def make_directory(request, site_id):
    return _operation(request, site_id, "mkdir", mutation=True)


@file_api
@login_required
@require_POST
def delete_file(request, site_id):
    return _operation(request, site_id, "delete", DeleteFileForm, mutation=True)


@file_api
@login_required
@require_POST
def move_file(request, site_id):
    return _operation(request, site_id, "move", MoveFileForm, mutation=True)


@file_api
@login_required
@require_POST
def change_permissions(request, site_id):
    return _operation(request, site_id, "chmod", ChmodFileForm, mutation=True)


@file_api
@login_required
@require_POST
def upload_files(request, site_id):
    site = _site(request, site_id)
    form = PathForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": "Choose a valid upload folder."}, status=400)
    files = request.FILES.getlist("files")
    if not files or len(files) > 100:
        return JsonResponse({"error": "Choose between 1 and 100 files."}, status=400)
    if sum(file.size for file in files) > MAX_UPLOAD_BYTES:
        return JsonResponse({"error": "Each upload may total at most 64 MiB."}, status=413)
    uploaded = []
    try:
        for file in files:
            path = clean_site_path("/".join(filter(None, (form.cleaned_data["path"], file.name))))
            encoded = base64.b64encode(file.read(MAX_UPLOAD_BYTES + 1)).decode("ascii")
            result = _request(
                site,
                "upload",
                {"path": path, "content_base64": encoded, "create_only": True},
                mutation=True,
            )
            uploaded.append(result["path"])
    except FileBackendError as exc:
        return JsonResponse({"error": str(exc), "uploaded": uploaded}, status=exc.status)
    except (ValueError, ValidationError):
        return JsonResponse(
            {"error": "An uploaded filename is invalid.", "uploaded": uploaded}, status=400
        )
    return JsonResponse({"uploaded": uploaded})


def _download(request, site_id, action):
    site = _site(request, site_id)
    form = PathForm(request.GET)
    if not form.is_valid():
        return JsonResponse({"error": "Choose a valid file path."}, status=400)
    path = form.cleaned_data["path"]
    payload = {"site": site.serialize_for_appserver(), "path": path}
    upstream = None
    try:
        for server in _servers():
            try:
                response = requests.post(
                    f"{server.protocol()}://{server.host}/api/files/{action}",
                    json=payload,
                    headers=server.auth_headers(),
                    stream=True,
                    timeout=(5, 300),
                )
                _response_error(response)
                upstream = response
                break
            except requests.RequestException:
                continue
            except FileBackendError as exc:
                response.close()
                if exc.status < 500:
                    raise
        if upstream is None:
            raise FileBackendError("The file service is unavailable. Try again later.")
    except FileBackendError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)

    name = path.rsplit("/", 1)[-1] or site.name
    response = StreamingHttpResponse(
        DownloadStream(upstream),
        content_type="application/zip" if action == "archive" else "application/octet-stream",
    )
    response["Content-Disposition"] = content_disposition_header(
        as_attachment=True, filename=name + ".zip" if action == "archive" else name
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store"
    return response


class DownloadStream:
    def __init__(self, response: requests.Response):
        self.response = response

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self.response.iter_content(chunk_size=65536)
        finally:
            self.close()

    def close(self):
        self.response.close()


@file_api
@login_required
@require_GET
def download_file(request, site_id):
    return _download(request, site_id, "download")


@file_api
@login_required
@require_GET
def download_archive(request, site_id):
    return _download(request, site_id, "archive")
