"""Seed only this preview's SQLite and filesystem with explicitly fake demo data."""

import json
from pathlib import Path

import django

django.setup()

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from director.apps.users.models import User
from director.apps.sites.models import (
    Action, Database, DatabaseHost, DockerImage, DockerImageSetupCommand,
    Domain, Operation, Site, SitePendingUser, SiteRequest, SiteResourceLimits,
)

PREVIEW_ROOT = Path(__file__).resolve().parent
assert Path(settings.DATABASES["default"]["NAME"]).resolve() == PREVIEW_ROOT / "preview.sqlite3"
assert settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend"
assert settings.DIRECTOR_APPSERVER_HOSTS == []
PASSWORD = "director-preview-only"


def demo_user(username, first, last, **roles):
    user, _ = User.objects.update_or_create(username=username, defaults={
        "first_name": first, "last_name": last, "email": f"{username}@example.test",
        "accepted_guidelines": True, "is_active": True, "graduation_year": None,
        **roles,
    })
    if not user.check_password(PASSWORD):
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    return user


with transaction.atomic():
    member = demo_user("preview", "Alex", "Morgan", is_student=True)
    collaborator = demo_user("member", "Jordan", "Lee", is_student=True)
    teacher = demo_user("teacher", "Taylor", "Reed", is_teacher=True)
    administrator = demo_user("admin", "Sam", "Rivera", is_staff=True, is_superuser=True)
    newcomer = demo_user("newcomer", "Casey", "Park", is_student=True)
    newcomer.accepted_guidelines = False
    newcomer.save(update_fields=["accepted_guidelines"])

    install, _ = DockerImageSetupCommand.objects.update_or_create(
        name="Install sample dependencies", defaults={"command": "python -m pip install -r requirements.txt", "order": 0}
    )
    python_image, _ = DockerImage.objects.update_or_create(name="python:3.13-slim", defaults={
        "friendly_name": "Python", "description": "A demo Python application image.",
        "is_user_visible": True, "install_command_prefix": "python -m pip install",
        "run_script_template": "#!/bin/sh\nexec python app.py\n",
    })
    python_image.setup_commands.set([install])
    node_image, _ = DockerImage.objects.update_or_create(name="node:22-alpine", defaults={
        "friendly_name": "Node.js", "description": "A demo JavaScript application image.",
        "is_user_visible": True, "install_command_prefix": "npm install",
        "run_script_template": "#!/bin/sh\nexec npm start\n",
    })
    node_image.setup_commands.clear()

    postgres, _ = DatabaseHost.objects.update_or_create(hostname="postgres.preview.invalid", port=5432, defaults={
        "dbms": "postgres", "admin_hostname": "internal-postgres.preview.invalid",
        "admin_username": "demo-administrator", "admin_password": "demo-admin-password-not-a-real-credential",
    })
    mysql, _ = DatabaseHost.objects.update_or_create(hostname="mysql.preview.invalid", port=3306, defaults={
        "dbms": "mysql", "admin_hostname": "internal-mysql.preview.invalid",
        "admin_username": "demo-administrator", "admin_password": "demo-admin-password-not-a-real-credential",
    })

    rows = [
        ("portfolio", "Projects, notes, and a little corner of the web.", "static", "user", "enabled", None),
        ("robotics-club", "Club news, competition results, and our latest builds.", "dynamic", "activity", "enabled", python_image),
        ("science-fair", "Project information and resources for the annual science fair.", "static", "project", "not-served", None),
        ("student-api", "A sample JSON API for student projects.", "dynamic", "project", "enabled", node_image),
        ("build-review", "A sample site showing a failed build and recovery guidance.", "dynamic", "project", "enabled", python_image),
        ("administrator-archive", "An administrator-only archived website.", "static", "legacy", "disabled", None),
    ]
    sites = {}
    for name, description, mode, purpose, availability, image in rows:
        site, _ = Site.objects.update_or_create(name=name, defaults={
            "description": description, "mode": mode, "purpose": purpose,
            "availability": availability, "docker_image": image,
            "admin_comments": "Demo administrator guidance shared with site members.",
        })
        site.users.set([administrator] if availability == "disabled" else [member])
        sites[name] = site
    sites["robotics-club"].users.add(collaborator, teacher)
    sites["student-api"].users.add(collaborator)
    SiteResourceLimits.objects.update_or_create(site=sites["robotics-club"], defaults={
        "cpus": 1.0, "memory": "256m", "max_request_body_size": 8 * 1024 * 1024,
        "notes": "Demo resource allocation; no service has been deployed.",
    })
    pending, _ = SitePendingUser.objects.get_or_create(username="future-member")
    pending.sites.set([sites["robotics-club"]])

    for name, host in [("robotics-club", postgres), ("student-api", mysql)]:
        site = sites[name]
        if site.database is None:
            site.database = Database.objects.create(host=host, password="demo-database-password-not-a-real-credential", provisioned=True)
            site.save(update_fields=["database"])
        else:
            site.database.host = host
            site.database.provisioned = True
            site.database.password = "demo-database-password-not-a-real-credential"
            site.database.pending_password = ""
            site.database.save()
    for site_name, domain, status in [
        ("robotics-club", "robotics.example.test", "active"),
        ("robotics-club", "retired-robotics.example.test", "inactive"),
        ("student-api", "api.example.test", "active"),
    ]:
        Domain.objects.update_or_create(domain=domain, defaults={
            "site": sites[site_name], "creating_user": administrator, "status": status,
        })
    Domain.objects.update_or_create(domain="reserved.example.test", defaults={"site": None, "status": "blocked"})

    failed_site = sites["build-review"]
    existing = Operation.objects.filter(site=failed_site).first()
    if existing:
        existing.action_set.all().delete()
        existing.delete()
    operation = Operation.objects.create(site=failed_site, ty="fix_site", started_time=timezone.now())
    Action.objects.create(operation=operation, slug="build_docker_image", name="Building Docker image", result=False,
        started_time=timezone.now(), user_recoverable=True,
        user_message="Demo failure: the application build needs review. Check its files, then retry.",
        message="Demo private diagnostic: the build command failed while installing sample dependencies.")

    for activity, approval, admin_decision in [
        ("Robotics outreach", None, None), ("Student research", True, None), ("Completed event", True, True),
    ]:
        SiteRequest.objects.update_or_create(user=member, teacher=teacher, activity=activity, defaults={
            "teacher_approval": approval, "admin_approval": admin_decision,
            "extra_information": "A seeded request for review of the interface.",
            "admin_comments": "Demo public review comments." if admin_decision is True else "",
            "private_admin_comments": "Demo private reviewer note.",
        })

file_samples = {
    "README.md": "# Read-only Director sample\n\nThese are preview files. Saving, uploading, deleting, or deployment is unavailable.\n",
    "public/index.html": "<!doctype html>\n<html lang=\"en\">\n  <head><title>Sample website</title></head>\n  <body><h1>Hello from the sample website</h1></body>\n</html>\n",
    "public/styles.css": "body { font-family: system-ui; max-width: 48rem; margin: 3rem auto; }\nh1 { color: #171717; }\n",
    "app.py": "# Read-only sample application; never started by this preview.\nfrom http.server import HTTPServer, SimpleHTTPRequestHandler\n\nif __name__ == '__main__':\n    HTTPServer(('0.0.0.0', 8000), SimpleHTTPRequestHandler).serve_forever()\n",
    "requirements.txt": "# Sample dependency file\nDjango>=5.2\n",
    "run.sh": "#!/bin/sh\n# Read-only sample.\nexec python app.py\n",
    "notes/competition-plan.md": "# Competition plan\n\n- Share team updates\n- Review the next build\n- Publish event results\n",
}
for site in sites.values():
    root = PREVIEW_ROOT / "fixture_files" / str(site.pk)
    for relative, content in file_samples.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o755 if relative.endswith(".sh") else 0o644)
    (root / "public" / "sample-image.bin").write_bytes(b"\x00READ-ONLY-BINARY-SAMPLE")
manifest = {"sites": {name: site.pk for name, site in sites.items()}, "accounts": ["preview", "member", "teacher", "admin", "newcomer"], "password": PASSWORD}
(PREVIEW_ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps(manifest, indent=2))
