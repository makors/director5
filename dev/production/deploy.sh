#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -f .env ]]; then
    python3 init-secrets.py
    echo 'Configure .env with the Ion OAuth client and rerun deploy.sh.' >&2
    exit 1
fi
chmod 600 .env
# Compose validation does not display interpolated secrets.
docker compose --env-file .env config --quiet
python3 - <<'PY'
from pathlib import Path
values = dict(line.split('=', 1) for line in Path('.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
missing = [key for key in ('SOCIAL_AUTH_ION_KEY', 'SOCIAL_AUTH_ION_SECRET') if not values.get(key, '').strip()]
if missing:
    raise SystemExit('Configure Ion OAuth before deployment: ' + ', '.join(missing))
PY
docker info >/dev/null
python3 preflight.py
director_swarm_state="$(docker info --format '{{.Swarm.LocalNodeState}}')"
if [[ "$director_swarm_state" != active ]]; then
    echo 'Initialize the intended single-node Docker Swarm before deployment.' >&2
    exit 1
fi
if [[ "$(docker info --format '{{.Swarm.ControlAvailable}}')" != true ]]; then
    echo 'Run Orchestrator on a Docker Swarm manager node.' >&2
    exit 1
fi
if ! docker network inspect director-sites >/dev/null 2>&1; then
    docker network create --driver overlay --attachable director-sites >/dev/null
fi
python3 - <<'PY'
import json
import subprocess
network = json.loads(subprocess.check_output(['docker', 'network', 'inspect', 'director-sites']))[0]
if network['Driver'] != 'overlay' or not network.get('Attachable'):
    raise SystemExit('Existing director-sites network must be an attachable overlay network.')
PY
(umask 022; mkdir -p /data/sites state)
(umask 077; mkdir -p /data/director-runtime-config state/ssh state/acme)
if [[ ! -f state/ssh/ssh_host_ed25519_key ]]; then
    ssh-keygen -q -t ed25519 -N '' -f state/ssh/ssh_host_ed25519_key
fi
chmod 600 state/ssh/ssh_host_ed25519_key
if [[ ! -f state/acme/acme.json ]]; then
    install -m 600 /dev/null state/acme/acme.json
fi
chmod 600 state/acme/acme.json
docker compose --env-file .env build init
# Application defaults still use nginx:latest and alpine:latest for hosted sites.
docker pull nginx:latest
docker pull alpine:latest
docker pull postgres:17-alpine
docker pull mysql:8.4
docker compose --env-file .env run --rm init
docker compose --env-file .env run --rm --no-deps init python manage.py check --deploy
docker compose --env-file .env up -d --wait --wait-timeout 240
python3 - <<'PY'
import json
from pathlib import Path
Path('state/installation.json').write_text(json.dumps({'project': 'director', 'sites_root': '/data/sites'}) + '\n')
PY
director_deploy_dir="$(pwd -P)"
# Only the DNS watcher is installed as a system service; Compose owns the stack.
python3 - "$director_deploy_dir" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
if any(char.isspace() or char in '\\"%' for char in str(root)):
    raise SystemExit('The deployment directory has unsupported systemd path characters.')
source = (root / 'director-dns-ready.service').read_text()
Path('/etc/systemd/system/director-dns-ready.service').write_text(source.replace('/opt/director5-production', str(root)))
PY
install -m 0644 director-dns-ready.timer /etc/systemd/system/director-dns-ready.timer
systemctl daemon-reload
systemctl enable --now director-dns-ready.timer
systemctl start director-dns-ready.service
docker compose --env-file .env ps
