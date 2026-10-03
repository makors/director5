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
