# Director5
A website hosting platform for the masses.

You can find more info on [our website](https://jasongrace2282.github.io/director5).


This project is the successor to:

- [Director3](https://github.com/tjcsl/director)
- [Director4](https://github.com/tjcsl/director4)

It focuses more on better error messages, and reducing points of
failure by incorporating newer technology for dynamic routing.

The Manager provides a simple light interface with a text-only Director name.
Site members can create personal or project sites, search and paginate their
sites, manage members and domains, switch site type, select approved images and
packages, and restart, rebuild, or delete a site. Failed hosting work remains
visible with a safe retry rather than being reported as successful.

Files include a directory browser, multiple editor tabs, syntax highlighting,
saved editor preferences, conflict-aware saves, multi-file upload, move, chmod,
deletion, downloads, and ZIP archives. Runtime tools show actual process status
and bounded, refreshed logs. Browser terminals and external SSH open disposable
site workspaces using the site's image and files. PostgreSQL and MySQL flows
include provisioning, connection credentials, SQL queries and terminals,
password rotation, and confirmed deletion.

Director4 governance flows include persisted guideline acceptance, student
requests with teacher and administrator review, personal-site restrictions,
administrative availability and resource limits, operation diagnostics, restricted
metrics, built-in documentation search, and mass-email drafts, confirmation, and
history. See [the capability and verification notes](docs/source/parity.md) for
operational requirements and deliberate differences from Director4.

Sign-in uses the same Ion OAuth provider and profile identity as Director4.
Configure `SOCIAL_AUTH_ION_KEY` and `SOCIAL_AUTH_ION_SECRET` in
`manager/director/settings/secret.py`, using `secret.sample` as the template, and
register `/social-auth/complete/ion/` on the Manager's public origin as the OAuth
callback. Development password login is available only while `DEBUG` is enabled.

See the [development setup guide](docs/source/contributing/developing/setup.md)
for the Manager, Celery, Redis, appserver, and Docker Swarm setup. Creation and
deployment require those services; UI previews with mocked workers do not verify
actual hosted sites. Static UI previews do not run Ion login, workers, databases,
terminals, or deployment operations.
