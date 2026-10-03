"""Idempotently install database offerings, approved images, and reserved domains."""

import os

from django.contrib.auth import get_user_model
from django.db import transaction

from director.apps.sites.models import DatabaseHost, DockerImage, Domain

with transaction.atomic():
    for engine, hostname, port, username, password_variable in (
        ("postgres", "director-site-postgres", 5432, "director_site_admin", "DIRECTOR_SITE_POSTGRES_PASSWORD"),
        ("mysql", "director-site-mysql", 3306, "root", "DIRECTOR_MYSQL_ROOT_PASSWORD"),
    ):
        DatabaseHost.objects.update_or_create(
            dbms=engine,
            hostname=hostname,
            port=port,
            defaults={
                "admin_hostname": hostname,
                "admin_port": port,
                "admin_username": username,
                "admin_password": os.environ[password_variable],
            },
        )
    for name, friendly_name, description, installer, script in (
        (
            "alpine:3.21", "Alpine Linux", "Small Linux workspace with BusyBox tools.",
            "apk add --no-cache", '#!/bin/sh\nexec busybox httpd -f -p "$PORT" -h /site/public\n',
        ),
        (
            "python:3.13-alpine", "Python 3.13", "Python applications and package installation.",
            "pip install", '#!/bin/sh\nexec python -m http.server "$PORT" --bind "$HOST"\n',
        ),
        (
            "node:22-alpine", "Node.js 22", "Node.js applications with npm packages.",
            "npm install", "#!/bin/sh\nexec node /site/public/app.js\n",
        ),
        (
            "php:8.4-cli-alpine", "PHP 8.4", "PHP applications served from the public directory.",
            "", '#!/bin/sh\nexec php -S "$HOST:$PORT" -t /site/public\n',
        ),
    ):
        DockerImage.objects.get_or_create(
            name=name,
            defaults={
                "friendly_name": friendly_name,
                "description": description,
                "is_user_visible": True,
                "install_command_prefix": installer,
                "run_script_template": script,
            },
        )
    for domain in ("director.makors.xyz", "sites.makors.xyz"):
        row, _ = Domain.objects.get_or_create(domain=domain, defaults={"status": "blocked"})
        if row.site_id is not None or row.status != "blocked":
            raise RuntimeError(f"The reserved domain {domain} is already assigned; resolve before deploying.")

    username = os.environ.get("DIRECTOR_ADMIN_ION_USERNAME", "").strip()
    email = os.environ.get("DIRECTOR_ADMIN_ION_EMAIL", "").strip()
    if username or email:
        if not username or not email:
            raise RuntimeError("Both administrator Ion username and Ion email are required.")
        user, created = get_user_model().objects.get_or_create(
            username=username,
            defaults={"email": email, "first_name": "Director", "last_name": "Administrator"},
        )
        if user.email.casefold() != email.casefold():
            raise RuntimeError("The administrator username has a different email; resolve manually.")
        if created:
            user.set_unusable_password()
        user.is_staff = user.is_superuser = True
        user.save()

print("Production database offerings, image catalog, and reserved domains are ready.")
