"""Export read-only sample payloads for static preview adapters, with no backend calls."""

import json
from pathlib import Path

import django

django.setup()

from django.conf import settings
from director.apps.sites.models import Site
from director.apps.sites.status import status_context
from director.apps.users.models import User
from parity_preview_apps import sample_files

ROOT = Path(__file__).resolve().parent
assert Path(settings.DATABASES["default"]["NAME"]).resolve() == ROOT / "preview.sqlite3"
assert settings.DIRECTOR_APPSERVER_HOSTS == []
assert settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend"
member = User.objects.get(username="preview")
payload = {"preview": True, "readonly": True, "sites": {}}
for site in Site.objects.filter_visible(member).order_by("name"):
    context = status_context(site, member)
    operation = context["operation"]
    root = ROOT / "fixture_files" / str(site.pk)
    listings, files = {}, {}
    for directory in [root, *sorted(path for path in root.rglob("*") if path.is_dir())]:
        path = "" if directory == root else directory.relative_to(root).as_posix()
        listings[path] = sample_files(site, "list", {"path": path})
    for file in sorted(path for path in root.rglob("*") if path.is_file() and path.suffix != ".bin"):
        path = file.relative_to(root).as_posix()
        files[path] = sample_files(site, "read", {"path": path})
    payload["sites"][site.name] = {
        "id": site.pk,
        "file_list": listings,
        "file_read": files,
        "file_mutation": {"status": 503, "body": {"error": "This is a read-only sample preview. File changes are unavailable."}},
        "runtime_status": {"status": 503, "body": {"state": "unavailable", "message": "Runtime is unavailable in this demo preview."}},
        "database_query": {"status": 503, "body": {"error": "Database queries are unavailable in this demo preview."}},
        "operation": None if operation is None else {
            "kind": operation.ty, "kind_label": operation.kind_label,
            "status": operation.status, "status_label": operation.status_label,
            "progress": operation.progress,
            "actions": [{"name": action.name, "result": action.result, "user_message": action.user_message} for action in context["actions"]],
        },
    }
target = ROOT / "fixture-responses.json"
target.write_text(json.dumps(payload, indent=2) + "\n")
print(f"Exported read-only file/runtime/query/status payloads for {len(payload['sites'])} demo sites.")
