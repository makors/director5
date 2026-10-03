#!/usr/bin/env python3
"""Refuse to replace unrelated listeners or pre-existing site storage."""

import json
import socket
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
expected_services = {80: "traefik", 443: "traefik", 2222: "ssh"}
own_ports = set()
containers = subprocess.check_output(["docker", "ps", "-q"], text=True).split()
for container in containers:
    template = '{{.Name}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{json .HostConfig.PortBindings}}'
    record = subprocess.check_output(["docker", "inspect", "--format", template, container], text=True)
    name, project, service, bindings = record.strip().split("|", 3)
    for entries in (json.loads(bindings) or {}).values():
        for binding in entries or []:
            port = int(binding["HostPort"])
            if port in expected_services:
                if project != "director" or service != expected_services[port]:
                    raise SystemExit(f"Port {port} is published by unrelated container {name}; retain it and resolve routing first.")
                own_ports.add(port)

if subprocess.check_output(["docker", "info", "--format", "{{.Swarm.ControlAvailable}}"], text=True).strip() == "true":
    service_ids = subprocess.check_output(["docker", "service", "ls", "-q"], text=True).split()
    for service_id in service_ids:
        template = '{{.Spec.Name}}|{{json .Endpoint.Spec.Ports}}'
        record = subprocess.check_output(["docker", "service", "inspect", "--format", template, service_id], text=True)
        name, ports = record.strip().split("|", 1)
        for port in json.loads(ports) or []:
            if int(port["PublishedPort"]) in expected_services:
                raise SystemExit(f"Public port {port['PublishedPort']} is already published by Swarm service {name}.")

for port in expected_services:
    if port in own_ports:
        continue
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            listener = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        with listener:
            if family == socket.AF_INET6:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                listener.bind((address, port))
            except OSError as error:
                if family == socket.AF_INET6 and error.errno in (97, 99):
                    continue
                raise SystemExit(f"Port {port} is unavailable: {error}. Resolve the existing listener before deployment.") from None

marker = root / "state" / "installation.json"
known_installation = marker.exists() and json.loads(marker.read_text()) == {"project": "director", "sites_root": "/data/sites"}
if not known_installation:
    for location in (Path("/data/sites"), Path("/data/director-runtime-config")):
        if location.is_symlink() or (location.exists() and not location.is_dir()):
            raise SystemExit(f"Existing {location} must be inspected before deployment.")
        if location.is_dir() and next(location.iterdir(), None) is not None:
            raise SystemExit(f"Existing nonempty {location} is not owned by this installation. Inspect and migrate it before deployment.")
print("Public ports and local site storage preflight passed.")
