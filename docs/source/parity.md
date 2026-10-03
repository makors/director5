# Director4 capabilities and verification

The reference is `tjcsl/director4` at tree
`c3f0e386e13f1098be6bda2987dc6bf48f3c5350`. Director5 keeps its simple,
text-only light interface rather than copying Director4's presentation.

| Capability                  | Director5 implementation                                                                                                                                                                                 |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Identity                    | Ion OAuth login, profile and role sync, guideline acceptance, pending membership attachment; permission checks on HTTP and WebSocket actions                                                             |
| Site discovery and creation | Structured AND search, filters, 30-row pagination, administrator Own/All, project and personal creation, agreement, membership selection, reserved-name validation                                       |
| Files                       | Multiple editor sessions, syntax highlighting, persisted preferences, save conflict detection, binary and multiple uploads, create/move/chmod/delete, download and directory ZIP                         |
| Runtime                     | Real service status, refreshed bounded logs, browser workspace terminal, interactive SQL clients, external SSH                                                                                           |
| Databases                   | Configured PostgreSQL and MySQL hosts, provision, credentials, query results, environment variables, rotate password, confirmed deletion, failed-operation recovery                                      |
| Settings                    | Name, domains, description, type, members, administrator purpose/comments and validated custom Nginx configuration                                                                                       |
| Images                      | Approved image catalog, ordered setup commands, packages, optional sample run script, administrator catalog editing, rebuild recovery                                                                    |
| Governance                  | Guideline gate and acceptance, student requests, assigned teacher decision, administrator processing, separate public/private comments and notifications                                                 |
| Administration              | Availability, CPU/memory/body limits and reasons, diagnostics, failed-operation retries, configuration reapply, restricted metrics, documentation reader/search, mass email preview/confirmation/history |

These are application capabilities, not a claim that a particular production
installation has been configured or that the static UI preview runs hosting.
Automated permission, state-transition, transport, and regression tests cover
the Manager and Orchestrator. Disposable real-backend checks cover filesystem
HTTP operations, PostgreSQL and MySQL lifecycle, Nginx configuration validation,
and Docker runtime tools. Production Ion sign-in, DNS/TLS, and the full deployed
Swarm routing flow still require the installation's credentials and services.

## Operational configuration

- Run the Manager, Celery workers, Redis, authenticated Orchestrators, Docker
  Swarm and its `director-sites` network, and the configured routing service.
- Set `DIRECTOR_DEBUG=false` in production Orchestrators and configure matching
  `DIRECTOR_APPSERVER_TOKEN` values. Use the Manager's existing TLS configuration
  for private transport; do not expose an unauthenticated Docker API.
- Configure Ion client key, secret, and the public callback described in the
  README. Password login is available only in development.
- Configure each DatabaseHost with its advertised hostname/port and private
  provisioning credentials. The advertised address must be reachable from both
  hosted sites and SQL terminal helpers; `admin_hostname` is used only by the
  provisioning/query service. User terminals never receive administrator
  database credentials.
- Set `RUNTIME_HELPER_NETWORK` to a network that reaches the advertised database
  hosts. Defaults are `bridge`, Alpine for shell fallback, PostgreSQL 17 and
  MySQL 8.4 for dedicated clients; helper images are configurable. Workspace
  terminals prefer the cached built site image. Helpers have site-only mounts,
  resource bounds, no published ports, and are removed when the session closes.
- Start the Manager's `runssh` command separately for external SSH. Configure
  `DIRECTOR_SSH_HOST`, `DIRECTOR_SSH_PORT`, a persistent `DIRECTOR_SSH_HOST_KEY`,
  and shared Redis cache across Manager and SSH processes.
- Set `DIRECTOR_MANAGER_URL` to the public Manager origin and configure a mail
  backend plus `DIRECTOR_CONTACT_EMAIL`. Notifications are best-effort after
  commit. Mass mail requires explicit confirmation. An ambiguous SMTP failure
  may already have delivered an email, so retrying can duplicate it.
- Package `docs/source` with the Manager, or set `DIRECTOR_DOCS_ROOT`. Configure
  allowed metrics scrape IPs using trusted direct client addresses.
- Apply migrations after taking a database backup. Resolve any existing active
  case-insensitive domain duplicates before applying the new domain uniqueness
  constraint. Existing database records are marked provisioned by the migration;
  new records retain pending provisioning state until confirmed success.

## Deliberate differences

SSH uses a one-time credential issued after Ion sign-in rather than Director4's
Kerberos shell authentication. It grants only a site workspace session, without
host shells or forwarding. Browser shells are disposable workspaces rather than
process attachment to the public website container. For static sites, installed
packages apply to that workspace; Nginx serves the public files.

Failed-operation reset binds to the confirmed operation and refuses running or
queued work. Director4's blind active-lock deletion is not reproduced because it
can race an active worker. Request approval does not automatically create a site,
matching Director4. A UI preview contains clearly labeled sample data and cannot
verify live Ion or hosting operations.
