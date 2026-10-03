#!/usr/bin/env bash
# One-time, reviewed installation. Usage: sudo ./install-deploy-access.sh public-key-file
set -euo pipefail
[[ $(id -u) == 0 && $# == 1 ]] || { echo 'Usage: sudo ./install-deploy-access.sh public-key-file' >&2; exit 1; }
public_key_path=$(realpath -- "$1")
cd "$(dirname "${BASH_SOURCE[0]}")"
[[ -f /opt/director5-production/state/installation.json ]] || { echo 'An existing Director installation is required.' >&2; exit 1; }
public_key=$(python3 - "$public_key_path" <<'PY'
import base64
import sys
from pathlib import Path
parts = Path(sys.argv[1]).read_text().strip().split()
if len(parts) not in (2, 3) or parts[0] != 'ssh-ed25519':
    raise SystemExit('Provide one Ed25519 public key, without authorized_keys options.')
base64.b64decode(parts[1], validate=True)
print(' '.join(parts[:2]))
PY
)
ssh-keygen -lf "$public_key_path" >/dev/null
if id director-deploy >/dev/null 2>&1; then
    echo 'director-deploy already exists; inspect its setup instead of overwriting it.' >&2
    exit 1
fi
install -o root -g root -m 0755 update-existing.py /usr/local/sbin/director5-update
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 deploy-ssh-entrypoint.py /usr/local/libexec/director5-deploy-ssh
# A valid login shell is needed for sshd forced commands; the account's key may
# invoke only the root-owned forced command and receives no interactive shell.
useradd --system --create-home --home-dir /var/lib/director-deploy --shell /bin/sh director-deploy
passwd -l director-deploy >/dev/null
chown root:root /var/lib/director-deploy
chmod 0755 /var/lib/director-deploy
install -d -o root -g root -m 0755 /var/lib/director-deploy/.ssh
printf 'restrict,command="/usr/local/libexec/director5-deploy-ssh" %s\n' "$public_key" > /var/lib/director-deploy/.ssh/authorized_keys
chown root:root /var/lib/director-deploy/.ssh/authorized_keys
chmod 0644 /var/lib/director-deploy/.ssh/authorized_keys
sudo_rule=$(mktemp)
trap 'rm -f "$sudo_rule"' EXIT
printf 'director-deploy ALL=(root) NOPASSWD: /usr/local/sbin/director5-update *\n' > "$sudo_rule"
visudo -cf "$sudo_rule" >/dev/null
install -o root -g root -m 0440 "$sudo_rule" /etc/sudoers.d/director5-deploy
printf 'Installed restricted deployment access for director-deploy.\n'
