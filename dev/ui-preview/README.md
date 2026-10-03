# Isolated UI preview

These tools recreate the sample preview for visual review. They use a separate
SQLite database, local sample files, in-memory email and channels, and disabled
hosting task dispatch. They do not authenticate with Ion or start hosted sites.
Never use these settings for production.

From the repository root, with the workspace dependencies installed:

```bash
export PYTHONPATH="$PWD/dev/ui-preview:$PWD/manager"
export DJANGO_SETTINGS_MODULE=parity_preview_settings
uv run python manager/manage.py migrate --noinput
uv run python dev/ui-preview/seed.py
uv run python manager/manage.py runserver 127.0.0.1:8083 --noreload
```

The sample usernames are `preview`, `member`, `teacher`, `admin`, and `newcomer`.
Their demo-only password is `director-preview-only`. Production does not use
these accounts. Sign in through the development password-login page.

To export the static sample UI, stop the development server and run:

```bash
uv run --with beautifulsoup4 python dev/ui-preview/export_preview.py
python3 -m http.server 8084 --bind 127.0.0.1 --directory dev/ui-preview/public
```

The static export includes member, administrator, and teacher routes. Files are
read-only; hosting, terminals, database queries, account changes, and email show
an unavailable message. Re-export after changing templates or styles. Rebuild
`manager/director/static/tailwind/build.css` with `dev/tailwind/build-tailwind.sh`
after changing Tailwind source; its standalone compiler must be installed first
as described in the development documentation.

Generated databases, files, manifests, and exports are ignored by Git. The source
adapters and fixture generator are included so the preview can be recreated.
