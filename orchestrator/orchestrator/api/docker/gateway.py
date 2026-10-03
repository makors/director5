"""Admin-defined Nginx rules stored outside the user's editable site directory."""

import os
import tempfile
from pathlib import Path
from typing import Any

import docker
from docker.types import Mount, Resources, ServiceMode

from orchestrator import settings

from .schema import SiteInfo
from .services import routing_labels


def config_path(site: SiteInfo, *, on_host: bool = False) -> Path:
    root = settings.HOST_SITES_DIR if on_host else settings.SITES_DIR
    return root.parent / "director-runtime-config" / f"{site.pk}.conf"


def prepare_gateway(client: docker.DockerClient, site: SiteInfo) -> Mount | None:
    if not site.custom_nginx_config:
        return None
    if site.type_ == "static":
        location = "root /usr/share/nginx/html;\ntry_files $uri $uri/ =404;"
    else:
        location = f"resolver 127.0.0.11 valid=10s;\nset $director_backend http://{site}:80;\nproxy_pass $director_backend;\nproxy_set_header Host $host;\nproxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;\nproxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;"
    content = f"server {{\nlisten 80;\nserver_name _;\nclient_max_body_size {site.resource_limits.max_request_body_size};\nlocation / {{\n{location}\n{site.custom_nginx_config}\n}}\n}}\n"
    target = config_path(site)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(handle, "w") as file:
            file.write(content)
        candidate = Mount(
            type="bind",
            source=str(config_path(site, on_host=True).parent / Path(temporary).name),
            target="/etc/nginx/conf.d/default.conf",
            read_only=True,
        )
        client.containers.run(
            "nginx:latest",
            ["nginx", "-t"],
            mounts=[candidate],
            network_mode="none",
            remove=True,
            mem_limit=64 * 1024 * 1024,
            nano_cpus=100_000_000,
        )
        # Publish only a validated candidate. A failed edit must retain the running config.
        Path(temporary).replace(target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return Mount(
        type="bind",
        source=str(config_path(site, on_host=True)),
        target="/etc/nginx/conf.d/default.conf",
        read_only=True,
    )


def gateway_params(site: SiteInfo, mount: Mount) -> dict[str, Any]:
    return {
        "name": f"{site}-proxy",
        "image": "nginx:latest",
        "mounts": [mount],
        "networks": ["director-sites"],
        "mode": ServiceMode(mode="replicated", replicas=int(site.is_served)),
        "labels": routing_labels(site, "80", str(site.resource_limits.max_request_body_size)),
        "resources": Resources(cpu_limit=100_000_000, mem_limit=64 * 1024 * 1024),
        "log_driver": "json-file",
        "log_driver_options": {"max-size": "500k", "max-file": "1"},
    }


def remove_config(site: SiteInfo) -> None:
    config_path(site).unlink(missing_ok=True)
