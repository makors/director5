#!/usr/bin/env python3
"""Start this stack's TLS edge after the two public DNS records resolve correctly."""

import os
import socket
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
os.chdir(root)
values = {}
for line in (root / ".env").read_text().splitlines():
    key, separator, value = line.partition("=")
    if separator and not key.startswith("#"):
        values[key] = value.strip()
expected = values.get("DIRECTOR_PUBLIC_IPV4", "135.148.41.70")
for hostname in ("director.makors.xyz", "director-dns-probe.sites.makors.xyz"):
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(hostname, 443, socket.AF_INET)}
    except socket.gaierror:
        print(f"Waiting for {hostname} DNS.")
        raise SystemExit(0) from None
    if addresses != {expected}:
        print(f"Waiting for {hostname} to resolve exclusively to the deployment server.")
        raise SystemExit(0)

subprocess.run(
    ["docker", "compose", "--env-file", ".env", "up", "-d", "traefik"],
    check=True,
)
(root / "state" / "dns-ready").touch(mode=0o600)
print("DNS is ready. The Director TLS edge has started.")
subprocess.run(["systemctl", "disable", "--now", "director-dns-ready.timer"], check=True)
