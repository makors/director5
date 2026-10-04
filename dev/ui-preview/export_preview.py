"""Export isolated Director demo pages; never export live user data or deploy."""
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urljoin, urlsplit

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "parity_preview_settings")
import django
django.setup()

from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import Client
from django.urls import reverse
from director.apps.sites import docs_views
from director.apps.sites.models import DockerImage, DockerImageSetupCommand, Site
from director.apps.users.models import MassEmail, User
from parity_preview_apps import PREVIEW_ROOT, sample_files
from director.apps.sites.file_views import FileBackendError

OUTPUT = Path(__file__).resolve().parent / "public"
assert Path(settings.DATABASES["default"]["NAME"]).resolve() == PREVIEW_ROOT / "preview.sqlite3"
assert settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend"
assert settings.DIRECTOR_APPSERVER_HOSTS == []
OUTPUT.mkdir(parents=True, exist_ok=True)
for adapter_name in ("preview.js", "preview.css"):
    shutil.copy2(Path(__file__).resolve().parent / adapter_name, OUTPUT / adapter_name)
clients = {}
for role, username in [("member", "preview"), ("admin", "admin"), ("teacher", "teacher"), ("newcomer", "newcomer")]:
    client = Client(HTTP_HOST="localhost")
    client.force_login(User.objects.get(username=username), backend="django.contrib.auth.backends.ModelBackend")
    clients[role] = client
clients["anonymous"] = Client(HTTP_HOST="localhost")
sites = list(Site.objects.order_by("id"))
assert set(site.name for site in sites) == {"portfolio", "robotics-club", "science-fair", "student-api", "build-review", "administrator-archive"}
member = User.objects.get(username="preview")
admin = User.objects.get(username="admin")
member_ids = set(member.site_set.values_list("pk", flat=True))
routes = {}
pages = []

def add(source, target, role="member"):
    routes[(role, source)] = target
    pages.append((source, target, role))

def mapped(source, role):
    if (role, source) in routes:
        return routes[(role, source)]
    for fallback in ("member", "admin", "teacher", "anonymous", "newcomer"):
        if (fallback, source) in routes:
            return routes[(fallback, source)]
    parsed = urlsplit(source)
    if parsed.query and mapped(parsed.path, role):
        return mapped(parsed.path, role) + "?" + parsed.query + ("#" + parsed.fragment if parsed.fragment else "")
    return None

add("/", "/")
add("/?all=1", "/preview/admin/", "admin")
routes[("admin", "/")] = "/preview/admin/"
add(reverse("sites:create"), "/sites/create/")
add(reverse("auth:login"), "/login/", "anonymous")
add(reverse("auth:password_login"), "/login/development/", "anonymous")
add(reverse("sites:request-create"), "/requests/new/")
add(reverse("sites:request-status"), "/requests/")
add(reverse("sites:request-teacher"), "/preview/teacher/requests/", "teacher")
add(reverse("sites:request-admin"), "/preview/admin/requests/", "admin")
add(reverse("sites:guidelines-read"), "/guidelines/")
add(reverse("sites:guidelines"), "/guidelines/accept/", "newcomer")

for site in sites:
    role = "member" if site.pk in member_ids else "admin"
    prefix = f"/sites/{site.name}/" if role == "member" else f"/preview/admin/sites/{site.name}/"
    add(reverse("sites:dashboard", args=[site.pk]), prefix, role)
    add(reverse("sites:settings", args=[site.pk]), prefix + "settings/", role)
    for action, suffix in [("sites:files:browser", "files/"), ("sites:terminal", "terminal/"), ("sites:logs", "logs/"), ("sites:ssh", "ssh/"), ("sites:image-select", "image/"), ("databases:detail", "database/")]:
        add(reverse(action, args=[site.pk]), prefix + suffix, role)
    add(reverse("sites:availability", args=[site.pk]), f"/preview/admin/sites/{site.name}/access/", "admin")
    add(reverse("sites:resource-edit", args=[site.pk]), f"/preview/admin/sites/{site.name}/resources/", "admin")
    if site.mode == "dynamic" and site.database_id:
        add(reverse("databases:query", args=[site.pk]), prefix + "database/query/", role)
        add(reverse("sites:terminal", args=[site.pk]) + "?kind=database", prefix + "database/terminal/", role)
    elif site.mode == "dynamic":
        add(reverse("databases:create", args=[site.pk]), prefix + "database/create/", role)
    if role == "member":
        add(reverse("sites:settings", args=[site.pk]), f"/preview/admin/sites/{site.name}/settings/", "admin")

for action, target in [("sites:management", "/preview/admin/management/"), ("sites:image-catalog", "/preview/admin/images/"), ("sites:image-create", "/preview/admin/images/new/"), ("sites:setup-create", "/preview/admin/images/setup/new/"), ("sites:resource-list", "/preview/admin/resources/"), ("sites:mail-history", "/preview/admin/email/"), ("sites:mail-compose", "/preview/admin/email/compose/")]:
    add(reverse(action), target, "admin")
add(reverse("sites:management") + "?failed=1", "/preview/admin/management/failed/", "admin")
for image in DockerImage.objects.all():
    add(reverse("sites:image-edit", args=[image.pk]), f"/preview/admin/images/{image.pk}/edit/", "admin")
    add(reverse("sites:image-delete", args=[image.pk]), f"/preview/admin/images/{image.pk}/delete/", "admin")
for command in DockerImageSetupCommand.objects.all():
    add(reverse("sites:setup-edit", args=[command.pk]), f"/preview/admin/images/setup/{command.pk}/edit/", "admin")
    add(reverse("sites:setup-delete", args=[command.pk]), f"/preview/admin/images/setup/{command.pk}/delete/", "admin")
for message in MassEmail.objects.all():
    add(reverse("sites:mail-detail", args=[message.request_id]), f"/preview/admin/email/{message.request_id}/", "admin")

documents = list(docs_views._documents())
for document in documents:
    add(document["url"], document["url"])
add(reverse("sites:docs-search"), "/docs/search/")
routes[("member", reverse("sites:create-personal"))] = "/sites/portfolio/"
routes[("admin", reverse("sites:create-personal"))] = "/sites/create/"
routes[("teacher", "/")] = "/"

datasets = {"files": {}, "docs": [{key: document[key] for key in ("title", "text", "url")} for document in documents]}
for site in sites:
    root = PREVIEW_ROOT / "fixture_files" / str(site.pk)
    sample = {"list": {}, "read": {}}
    for target in [root, *sorted(root.rglob("*"))]:
        path = "" if target == root else target.relative_to(root).as_posix()
        action = "list" if target.is_dir() else "read"
        try:
            value = sample_files(site, action, {"path": path})
            sample[action][path] = {"status": 200, "body": value}
        except FileBackendError as error:
            sample[action][path] = {"status": error.status, "body": {"error": str(error)}}
    datasets["files"][str(site.pk)] = sample

unavailable_links = set()
for source, canonical, role in pages:
    response = clients[role].get(source)
    if response.status_code in (301, 302):
        destination = mapped(response["Location"], role)
        assert destination, (source, response["Location"])
        soup = BeautifulSoup(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={destination}"></head><body><a href="{destination}">Continue</a></body></html>', "html.parser")
    else:
        assert response.status_code == 200, (source, role, response.status_code)
        soup = BeautifulSoup(response.content, "html.parser")
        for node in soup.find_all(True):
            for attribute in list(node.attrs):
                if attribute.startswith(("hx-", "ws-")) or attribute in ("data-dt-form", "data-dt-search"):
                    del node.attrs[attribute]
            for attribute in ("href", "src"):
                href = node.get(attribute, "")
                if not href or href.startswith(("#", "/static/", "http:", "https:", "mailto:", "data:")):
                    continue
                destination = mapped(urljoin(source, href), role)
                if destination:
                    node[attribute] = destination
                elif attribute == "href":
                    unavailable_links.add(href)
                    node["href"] = "#preview-notice"
                    node["data-preview-unavailable"] = "true"
            if node.name == "input" and node.get("name") == "csrfmiddlewaretoken":
                if node.find_parent(id="file-csrf"):
                    node["value"] = "preview-readonly"
                else:
                    node.decompose()
        for script in soup.find_all("script"):
            src = script.get("src", "")
            if any(name in src for name in ("ws.min.js", "htmx.min.js", "django-htmx", "dashboard.js")):
                script.decompose()
        for notice in soup.select("[data-director-preview]"):
            notice.decompose()
        notice = soup.new_tag("details", id="preview-notice", attrs={"class": "dt-preview-notice"})
        summary = soup.new_tag("summary")
        summary.string = "Preview · Sample data · Details"
        notice.append(summary)
        notice.append("Static UI preview · Sample data. Files are read-only. Hosting, account actions, terminals, database queries, and email require the live Director server. ")
        for text, href in [("Member preview", "/"), ("Admin preview", "/preview/admin/"), ("Teacher review", "/preview/teacher/requests/")]:
            link = soup.new_tag("a", href=href)
            link.string = text
            notice.append(link)
            notice.append(" ")
        soup.find("main").insert(0, notice)
        for form in soup.find_all("form"):
            if form.get("role") == "search" or form.get("id") in {"file-csrf", "file-dialog-form", "file-preferences-form", "file-editor-form"}:
                continue
            form["data-preview-action"] = "true"
            form.attrs.pop("action", None)
            form.attrs.pop("method", None)
        for link in soup.select(".dt-project-domain"):
            link["href"] = "#preview-notice"
            link["data-preview-unavailable"] = "true"
            link.attrs.pop("target", None)
        for input_id in ("database-password", "database-full-url"):
            field = soup.find(id=input_id)
            if field:
                field["value"] = "Unavailable in static preview"
        output = soup.find(id="runtime-log-output")
        if output:
            output.clear()
            message = soup.new_tag("p", attrs={"class": "dt-muted"})
            message.string = "Logs are unavailable in this static preview."
            output.append(message)
        for field in soup.select("[data-runtime-status-url]"):
            field.string = "Unavailable"
        for row in soup.select(".dt-site-list > li"):
            name = row.select_one("h2").get_text(strip=True)
            site = next(site for site in sites if site.name == name)
            row["data-preview-search"] = (site.name + " " + site.description).casefold()
            row["data-preview-type"] = site.mode
            row["data-preview-owned"] = str(site.users.filter(pk=admin.pk if role == "admin" else member.pk).exists()).lower()
        adapter = soup.new_tag("script", src="/preview.js")
        soup.head.insert(0, adapter)
        data = soup.new_tag("script", src="/preview-data.js")
        soup.head.insert(0, data)
        styles = soup.new_tag("link", rel="stylesheet", href="/preview.css")
        soup.head.append(styles)
    target = OUTPUT / canonical.strip("/") / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(soup), encoding="utf-8")

# Copy every registered static asset, including dynamic Ace modes, font/vendor licenses,
# and stylesheet dependencies. Ignore duplicate finder paths using first-wins semantics.
asset_count = 0
seen_assets = set()
for finder in finders.get_finders():
    for path, storage in finder.list([]):
        target = OUTPUT / "static" / path
        if path in seen_assets:
            continue
        seen_assets.add(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with storage.open(path, "rb") as source_file, target.open("wb") as output_file:
            shutil.copyfileobj(source_file, output_file)
        asset_count += 1
(OUTPUT / "preview-data.js").write_text("window.directorPreviewData = " + json.dumps(datasets, ensure_ascii=True) + ";\n")
manifest = {
    "pages": [{"source": source, "url": canonical, "role": role} for source, canonical, role in pages],
    "assets": asset_count,
    "sample_sites": [site.name for site in sites],
    "unavailable_links": sorted(unavailable_links),
    "files": {str(path.relative_to(OUTPUT)): path.stat().st_size for path in OUTPUT.rglob("*") if path.is_file()},
}
(Path(__file__).resolve().parent / "export-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps({"pages": len(pages), "assets": asset_count, "files": len(manifest["files"]), "bytes": sum(manifest["files"].values()), "unavailable_links": sorted(unavailable_links)}))
