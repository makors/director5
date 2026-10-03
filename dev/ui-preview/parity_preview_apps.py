"""Preview-only isolation and labeled sample-file reads; never deployed with the app."""

import hashlib
import re
import stat
from pathlib import Path

from celery.app.task import Task
from django.apps import AppConfig

PREVIEW_ROOT = Path(__file__).resolve().parent


def reject_task_dispatch(*args, **kwargs):
    raise RuntimeError("Hosting operations are unavailable in the isolated UI preview.")


def sample_files(site, action, data, *, mutation=False):
    from director.apps.sites.file_views import FileBackendError

    if mutation or action not in {"list", "read"}:
        raise FileBackendError("This is a read-only sample preview. File changes are unavailable.", 503)
    root = (PREVIEW_ROOT / "fixture_files" / str(site.pk)).resolve()
    target = (root / data.get("path", "")).resolve()
    if not target.is_relative_to(root):
        raise FileBackendError("Choose a path inside this sample site.", 400)
    if not root.is_dir():
        raise FileBackendError("Sample files are only available for the seeded demo sites.", 503)
    if not target.exists():
        raise FileBackendError("This sample file or folder does not exist.", 404)
    if action == "list":
        if not target.is_dir():
            raise FileBackendError("Choose a sample folder.", 400)
        entries = []
        for item in target.iterdir():
            info = item.stat()
            entries.append({
                "name": item.name, "path": item.relative_to(root).as_posix(),
                "type": "directory" if item.is_dir() else "file",
                "size": info.st_size, "modified": info.st_mtime,
                "mode": f"{stat.S_IMODE(info.st_mode) & 0o777:03o}",
            })
        return {
            "path": "" if target == root else target.relative_to(root).as_posix(),
            "entries": sorted(entries, key=lambda item: (item["type"] != "directory", item["name"].casefold())),
            "readonly": True, "preview": True,
        }
    if not target.is_file():
        raise FileBackendError("Choose a sample text file.", 400)
    content = target.read_bytes()
    if len(content) > 2 * 1024 * 1024:
        raise FileBackendError("This sample is too large for the text editor.", 413)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FileBackendError("This sample is binary data.", 415) from error
    if "\x00" in text:
        raise FileBackendError("This sample is binary data.", 415)
    return {
        "content": text, "sha256": hashlib.sha256(content).hexdigest(),
        "mode": f"{stat.S_IMODE(target.stat().st_mode) & 0o777:03o}",
        "readonly": True, "preview": True,
    }


class PreviewIsolation(AppConfig):
    name = "parity_preview_apps"

    def ready(self):
        from director.apps.sites import file_views

        Task.apply_async = reject_task_dispatch
        file_views._request = sample_files


class PreviewNotice:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.streaming or "text/html" not in response.get("Content-Type", ""):
            return response
        notice = (
            '<div role="status" data-director-preview="readonly" '
            'style="padding:8px 24px;border-bottom:1px solid #e5e7eb;background:#f8fafc;'
            'font:12px/1.5 system-ui;color:#475569">'
            'Demo data · Files are read-only. Hosting, database queries, and terminals are unavailable.'
            '</div>'
        )
        text = response.content.decode(response.charset)
        text = re.sub(r'(<main\b[^>]*>)', lambda match: match[0] + notice, text, count=1)
        response.content = text.encode(response.charset)
        return response
