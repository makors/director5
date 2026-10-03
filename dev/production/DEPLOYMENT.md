# Director production deployment

This bundle runs the Manager with Daphne ASGI, Celery, Redis, a private Manager
PostgreSQL database, separate hosted-site PostgreSQL and MySQL engines, the
authenticated Orchestrator, a site-only SSH gateway, and Traefik TLS routing.
It requires the current Director source including the corrected production
settings import order. It does not contain credentials or demo accounts.

Copy the source to `/opt/director5` and this directory to
`/opt/director5-production`. If another location is used, set the two directory
values in `.env`. Both directories are on the destination Linux host. Docker
Engine with the Compose v2 plugin and OpenSSH `ssh-keygen` must be installed.
The destination uses systemd to watch DNS readiness after deployment.

Traefik is pinned to `v3.7.13`, the supported release checked on October 3, 2026.
Docker Engine 29 requires a newer Docker API than Traefik 3.2's client uses;
[automatic API negotiation landed in 3.6.1](https://github.com/traefik/traefik/releases/tag/v3.6.1).
The [3.6 support window has ended](https://doc.traefik.io/traefik/deprecation/releases/),
so use the pinned [3.7.13 release](https://github.com/traefik/traefik/releases/tag/v3.7.13)
rather than lowering Docker's minimum API version or restarting the host daemon.
For an existing installation, validate Compose, pull `traefik`, then recreate
only that service with `docker compose --env-file .env up -d --no-deps traefik`.
Keep the existing ACME bind mount. The edge replacement briefly interrupts HTTP,
HTTPS, and open WebSockets; hosted containers and databases keep running.
Verify Manager and hosted-site HTTPS plus browser terminals after replacement.
The [minor-version migration notes](https://doc.traefik.io/traefik/migrate/v3/)
include stricter request-path normalization and header handling; applications
relying on ambiguous paths or unusual headers need particular attention.

Inspect the host's existing ports, Docker networks, Swarm state, `/data`, and
files before making changes. This configuration uses a single Docker Swarm
manager: hosted images are local to that node and site files are local bind
mounts. Do not add worker nodes without implementing image distribution,
placement constraints, and shared storage.
The preflight refuses unrelated listeners or Docker-published ports at 80, 443,
and 2222. It also refuses nonempty existing site/runtime directories without
this deployment's installation marker. It preserves unrelated `/data` content
and does not change existing directory permissions. Review and migrate any
conflicts before attempting deployment.

DNS records for the selected server:

| Type | Name | Value |
| --- | --- | --- |
| A | director.makors.xyz | 135.148.41.70 |
| A | *.sites.makors.xyz | 135.148.41.70 |

Only publish AAAA records if IPv6 actually reaches the same server. Every site
purpose uses `https://<name>.sites.makors.xyz`; no additional user/activity domain
is needed. Custom domains must resolve to the server before their deployment.
Traefik issues a separate Let's Encrypt certificate for each configured hostname
using HTTP-01. Wildcard DNS does not require a wildcard certificate or DNS API
credentials. DNS and inbound port 80 must work before certificate issuance.
The stack initially starts its internal services. A systemd timer checks both
DNS records once a minute and starts this stack's Traefik only after they resolve
exclusively to `DIRECTOR_PUBLIC_IPV4` (135.148.41.70 by default). It then disables
itself. This lets the owner add DNS after deployment without triggering early
ACME failures. It does not restart other stacks or reverse proxies. DNS proxying
must remain disabled for this exact-address readiness check.

Public ports are 80/tcp (ACME and HTTPS redirect), 443/tcp (Manager, hosted sites,
and browser WebSockets), and 2222/tcp (site-only SSH). Preserve the server's own
administrative SSH port. Manager, Orchestrator, Redis, PostgreSQL, and MySQL do
not publish host ports. Docker's network rules must be considered when configuring
the host firewall. A single-node Swarm needs no public peer ports.

Generate credentials directly on the destination:

```bash
cd /opt/director5-production
python3 init-secrets.py
```

Edit the private `.env` to set the Ion OAuth application key and secret. Register
the exact callback `https://director.makors.xyz/social-auth/complete/ion/` with Ion.
Set `DIRECTOR_ADMIN_ION_USERNAME` and `DIRECTOR_ADMIN_ION_EMAIL` to the owner's
actual Ion identity to provision administrator access. This creates an unusable
password and relies on Ion sign-in; production password login stays disabled.
If the owner already signed in, these variables promote that matching user.
Configure the SMTP settings if mass email will be used; email otherwise fails
visibly rather than being marked sent. Do not commit `.env` or private SSH keys.

Initialize a single-node Swarm only if the host is not already a Swarm manager,
using the server's intended advertise address. Review any existing `/data`
content first, then run the authorized deployment:

```bash
docker swarm init --advertise-addr 135.148.41.70
sudo ./deploy.sh
```

If Swarm is already active, skip `docker swarm init`. `deploy.sh` verifies the
network, builds the application image, preserves existing credentials and SSH
keys, migrates the Manager database, collects static files, registers database
offerings and approved images, reserves the Manager hostname, performs Django's
deployment check, and starts the stack. Repeated bootstrap runs preserve edited
image catalog entries. They synchronize configured database administrator
credentials but do not rotate actual database engine passwords.
The Django deployment check intentionally retains the warnings for disabling
HSTS subdomain coverage and preload: the Manager sends HSTS only for its own
hostname and does not enroll the domain in browser preload lists.
If DNS was already configured, the timer's immediate check starts Traefik during
deployment. Inspect `systemctl status director-dns-ready.timer` and
`journalctl -u director-dns-ready.service` while DNS is still pending.

Production Orchestrator hardcodes both its own and host-side site roots to
`/data/sites`. Its `/data:/data` bind mount is therefore deliberate. Docker
services mount `/data/sites/<id // 100>/<id % 100>` and gateway configurations
under `/data/director-runtime-config`. Hosted services and terminal helpers join
the `director-sites` attachable overlay network. Their database DNS names are
`director-site-postgres:5432` and `director-site-mysql:3306` on that network.
The Manager control database is isolated on `director-control`.

Persistent named volumes hold Manager PostgreSQL, hosted PostgreSQL, hosted
MySQL, Redis, collected static assets, and Manager media. Site files live under
`/data/sites`. `state/acme/acme.json` preserves issued TLS certificates and
`state/ssh` preserves the SSH host identity. Back up these, the private `.env`,
and Manager database together. Do not use `docker compose down -v` during normal
updates. Rotating database passwords requires updating the running engines and
`.env` together; changing environment variables alone cannot rotate an existing
database volume's credentials.

After DNS and OAuth are configured, verify real sign-in, administrator access,
site creation, HTTP-to-HTTPS routing, file save/upload/download, a Python or
Alpine dynamic site listening on `$PORT` (80), browser terminals, external site
SSH, both database engines, password rotation, and restart/rebuild/delete flows.
Use `docker compose logs manager celery orchestrator traefik` for failures and
`docker service ls` for hosted applications. Creating a dynamic site initially
uses the minimal Alpine default; select an approved image and a suitable
`run.sh` before expecting an application server. A local browser UI preview
does not establish that these production services work.

## Automatic application updates from GitHub

Pushing to `makors/director5` branch **`director5-handoff`** runs CI. A manual CI run on the same branch supports checked retries. Only a push or manual run
whose lint, Manager/Orchestrator tests, updater safety tests, and documentation
build all pass reaches the `production` deployment job. Pull requests and other
branches never receive deployment credentials. The job and server both check
that the tested full commit SHA is still the branch tip. Concurrent updates are
serialized in Actions and rejected while the server lock is held.

This updates application code only. The root-owned production Compose file,
private `.env`, bootstrap/settings mounts, network/database configuration,
Traefik, SSH host key, ACME state, media, and hosted-site files remain in place.
Changes to `dev/production` configuration or the updater itself require a
separate reviewed administrator installation. Do not use first-install
`deploy.sh` for routine application updates after enabling this workflow.

One-time setup (review these files before using sudo):

1. Create a dedicated Ed25519 key pair used only by this repository's deployment
   job. Keep the private key out of Git and out of application images.
2. Copy `install-deploy-access.sh`, `deploy-ssh-entrypoint.py`, and
   `update-existing.py`, plus the **public** key, to a private staging directory
   on the server. Run `sudo ./install-deploy-access.sh /absolute/public-key.pub`
   from that directory. The installer requires an existing Director installation
   and refuses to replace an existing `director-deploy` account.
3. In the GitHub repository's **production environment**, configure secrets
   `DIRECTOR_DEPLOY_SSH_KEY` (the dedicated private key) and
   `DIRECTOR_DEPLOY_KNOWN_HOSTS` (the host's verified administrative SSH host-key
   entry). Obtain the host key through the already trusted administrative
   connection; do not trust an unauthenticated keyscan. This is the server SSH
   listener, normally port 22, not Director's site SSH gateway on port 2222.
4. Optional environment variables `DIRECTOR_DEPLOY_HOST` and
   `DIRECTOR_DEPLOY_PORT` override `135.148.41.70` and `22`. A nonstandard port
   requires the matching `[hostname]:port` known-hosts entry. Restrict the
   environment to `director5-handoff`. Required environment reviewers pause
   automatic runs, so configure them only if manual deployment approval is wanted.
5. Protect the deployment branch and workflow files according to repository
   policy. Anyone allowed to push deployable code can change code running with
   Director's privileges; the forced SSH command narrows shell access, not the
   trust placed in application code. Trigger the first run by pushing the branch.

The account has a locked password, no Docker group membership, a root-owned
restricted `authorized_keys`, and no usable interactive SSH command. Its forced
command accepts only `deploy <40-character lowercase SHA>` and uses a narrow
sudo entry to invoke the root-owned updater. The updater itself validates the
argument again and downloads only the fixed repository's exact GitHub archive
over HTTPS. It rejects traversal, links, devices, duplicates, and oversized
archives. Server HTTPS access to GitHub and its public API is required; public
API rate limits fail closed before stopping the app.

Each update builds an image tagged with the tested SHA, snapshots the static
volume, stops only Manager, Celery, Orchestrator, and the site SSH gateway, then
backs up the Manager database with `pg_dump -Fc`. It runs the existing production
initialization command (migrations, static collection, catalog bootstrap),
Django deployment checks, and application health checks. The GitHub runner then
checks public Manager HTTPS. Existing hosted web containers and database engines
continue running. Manager and interactive sessions have a brief maintenance
interruption while these app services change.

Backups, source archives, and private logs are kept under
`/opt/director5-production/state/releases/<commit>-<unique>/`; only root can read
them. Successful deployment metadata is in `state/deployed-commit.json`. Disk
retention is administrator-managed; nothing automatically deletes old backups
or images. Protect these backups as credentials and personal data.

On a server-side failure after stopping the app, the updater attempts to restore
the previous application image and prior static files. Extra new static files
may remain but old paths are restored. **This is not a database rollback.**
Migrations may already have committed. Automatic deployments must use schema
changes compatible with both the old and new application versions; destructive
or incompatible migrations require a separately planned maintenance deployment.
If old-image health checks fail, stop and inspect the private log and database
backup. Never automatically restore a database over new user writes. A failure
of the runner's final public HTTPS check reports a failed job and requires
investigation; it does not revert a healthy server through an unrelated network
failure.

Image selection is retained in `state/application-image.yaml`. For manual app
inspection/restarts, include that override:

```bash
cd /opt/director5-production
sudo docker compose --env-file .env -f compose.yaml -f state/application-image.yaml ps
```

After updating the updater files themselves, an administrator must review and
replace `/usr/local/sbin/director5-update` and
`/usr/local/libexec/director5-deploy-ssh`; the existing-account installer guard is
intentional. Local safety checks require no server or Docker:

```bash
python3 -m unittest discover -s dev/production/tests -v
bash -n dev/production/install-deploy-access.sh
```
