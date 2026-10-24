"""Kubernetes objects for a site, mirroring the Docker Swarm service definition.

Each site gets a Deployment (one pod when served, none otherwise), a ClusterIP
Service and a Traefik IngressRoute. Pods run under ``KUBERNETES_RUNTIME_CLASS``,
which on production is Kata Containers, so every site gets its own lightweight VM.
"""

import string
from typing import Any

from orchestrator import settings

from ..docker.schema import SiteInfo
from ..docker.services import TEMPLATE_DIR

MANAGED_BY = {"app.kubernetes.io/managed-by": "director"}
TRAEFIK_API_VERSION = "traefik.io/v1alpha1"


def object_name(site_id: int, suffix: str = "") -> str:
    """Kubernetes names allow hyphens but not the underscores in ``site_0001``."""
    return f"site-{site_id:04d}{suffix}"


def site_labels(site_id: int, component: str) -> dict[str, str]:
    return {**MANAGED_BY, "director.site/id": str(site_id), "director.site/component": component}


def selector(site_id: int, component: str) -> dict[str, str]:
    return {"director.site/id": str(site_id), "director.site/component": component}


def service_dns_name(site_id: int) -> str:
    return f"{object_name(site_id)}.{settings.KUBERNETES_NAMESPACE}.svc.cluster.local"


def cpu_quantity(nano_cpus: int) -> str:
    return f"{max(nano_cpus // 1_000_000, 1)}m"


def runtime_class() -> dict[str, str]:
    name = settings.KUBERNETES_RUNTIME_CLASS
    return {"runtimeClassName": name} if name else {}


def memory_volume(name: str, size: int) -> dict[str, Any]:
    return {"name": name, "emptyDir": {"medium": "Memory", "sizeLimit": str(size)}}


def pod_defaults() -> dict[str, Any]:
    """Settings every Director pod shares. Sites never talk to the Kubernetes API."""
    return {
        **runtime_class(),
        "automountServiceAccountToken": False,
        # Service links would expose every other site's address as environment variables.
        "enableServiceLinks": False,
    }


def site_command(site: SiteInfo) -> list[str]:
    runfile = site.runfile
    if runfile is None:
        runfile = " ".join(("/site/run.sh", "/site/private/run.sh", "/site/public/run.sh"))
    template = string.Template((TEMPLATE_DIR / "run-site.sh").read_text())
    return ["sh", "-c", template.safe_substitute(SEARCH_PATH=runfile)]


def site_deployment(
    site: SiteInfo, image: str, restarted_at: str, nginx_config_path: str | None = None
) -> dict[str, Any]:
    """The site's workload. ``restarted_at`` forces a new pod on every deploy.

    ``nginx_config_path`` is a validated custom Nginx config for a static site.
    """
    host_dir = str(site.directory_path(on_host=True))
    env = {**site.container_env(), "PORT": "80", "HOST": "0.0.0.0"}
    mounts: list[dict[str, Any]]
    if site.type_ == "dynamic":
        mounts = [
            {"name": "site", "mountPath": "/site"},
            {"name": "site", "mountPath": "/root", "subPath": ".home"},
        ]
        container: dict[str, Any] = {
            "image": image,
            "command": site_command(site),
            "workingDir": "/site/public",
            "securityContext": {"runAsUser": 0, "readOnlyRootFilesystem": True},
        }
    else:
        mounts = [
            {
                "name": "site",
                "mountPath": "/usr/share/nginx/html",
                "subPath": "public",
                "readOnly": True,
            }
        ]
        if nginx_config_path is not None:
            mounts.append(
                {
                    "name": "nginx-config",
                    "mountPath": "/etc/nginx/conf.d/default.conf",
                    "readOnly": True,
                }
            )
        # Nginx needs a writable file system; static site owners cannot reach this container.
        container = {"image": settings.STATIC_SITE_IMAGE, "workingDir": "/usr/share/nginx/html"}
    limits = site.resource_limits
    container |= {
        "name": "site",
        "env": [{"name": name, "value": str(value)} for name, value in env.items()],
        "ports": [{"name": "http", "containerPort": 80}],
        "resources": {
            "limits": {"cpu": cpu_quantity(int(limits.cpus)), "memory": str(limits.memory)},
            # Small requests keep many idle sites schedulable on one node.
            "requests": {"cpu": "10m", "memory": str(min(limits.memory, 32 * 1024**2))},
        },
        "volumeMounts": [
            *mounts,
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "run", "mountPath": "/run"},
        ],
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": object_name(site.pk),
            "namespace": settings.KUBERNETES_NAMESPACE,
            "labels": site_labels(site.pk, "site"),
        },
        "spec": {
            "replicas": int(site.is_served),
            # Stop the old process before starting the new one, as the Swarm service did.
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector(site.pk, "site")},
            "template": {
                "metadata": {
                    "labels": site_labels(site.pk, "site"),
                    "annotations": {"director.site/restarted-at": restarted_at},
                },
                "spec": {
                    **pod_defaults(),
                    "terminationGracePeriodSeconds": 3,
                    "containers": [container],
                    "volumes": [
                        {"name": "site", "hostPath": {"path": host_dir, "type": "Directory"}},
                        memory_volume("tmp", settings.TMP_TMPFS_SIZE),
                        memory_volume("run", settings.RUN_TMPFS_SIZE),
                        *(
                            [
                                {
                                    "name": "nginx-config",
                                    "hostPath": {"path": nginx_config_path, "type": "File"},
                                }
                            ]
                            if nginx_config_path is not None
                            else []
                        ),
                    ],
                },
            },
        },
    }


def gateway_deployment(site: SiteInfo, config_host_path: str, restarted_at: str) -> dict[str, Any]:
    """An Nginx proxy applying an administrator's custom rules in front of the site."""
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": object_name(site.pk, "-proxy"),
            "namespace": settings.KUBERNETES_NAMESPACE,
            "labels": site_labels(site.pk, "proxy"),
        },
        "spec": {
            "replicas": int(site.is_served),
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector(site.pk, "proxy")},
            "template": {
                "metadata": {
                    "labels": site_labels(site.pk, "proxy"),
                    "annotations": {"director.site/restarted-at": restarted_at},
                },
                "spec": {
                    **pod_defaults(),
                    "terminationGracePeriodSeconds": 3,
                    "containers": [
                        {
                            "name": "proxy",
                            "image": settings.STATIC_SITE_IMAGE,
                            "ports": [{"name": "http", "containerPort": 80}],
                            "resources": {
                                "limits": {"cpu": "100m", "memory": str(64 * 1024**2)},
                                "requests": {"cpu": "10m", "memory": str(16 * 1024**2)},
                            },
                            "volumeMounts": [
                                {
                                    "name": "config",
                                    "mountPath": "/etc/nginx/conf.d/default.conf",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "config", "hostPath": {"path": config_host_path, "type": "File"}}
                    ],
                },
            },
        },
    }


def service(site_id: int, component: str = "site") -> dict[str, Any]:
    suffix = "" if component == "site" else f"-{component}"
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": object_name(site_id, suffix),
            "namespace": settings.KUBERNETES_NAMESPACE,
            "labels": site_labels(site_id, component),
        },
        "spec": {
            "type": "ClusterIP",
            "selector": selector(site_id, component),
            "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
        },
    }


def route_rules(site: SiteInfo) -> list[tuple[str, str, str]]:
    """(name, Traefik rule, path prefix) for each public route, as the Swarm labels use."""
    if site.routes is None:
        return [("", " || ".join(f"Host(`{host}`)" for host in site.hosts), "/")]
    rules = []
    for index, route in enumerate(site.routes):
        rule = f"Host(`{route.host}`)"
        if route.path_prefix != "/":
            prefix = route.path_prefix.rstrip("/")
            # Exact matches plus a trailing slash avoid matching another site's name.
            rule += f" && (Path(`{prefix}`) || PathPrefix(`{route.path_prefix}`))"
        rules.append((f"-{index}", rule, route.path_prefix))
    return rules


def middlewares(site: SiteInfo) -> list[dict[str, Any]]:
    namespace = settings.KUBERNETES_NAMESPACE
    result = [
        {
            "apiVersion": TRAEFIK_API_VERSION,
            "kind": "Middleware",
            "metadata": {
                "name": object_name(site.pk, "-body"),
                "namespace": namespace,
                "labels": site_labels(site.pk, "route"),
            },
            "spec": {
                "buffering": {"maxRequestBodyBytes": site.resource_limits.max_request_body_size}
            },
        }
    ]
    for suffix, _rule, path_prefix in route_rules(site):
        if path_prefix != "/":
            result.append(
                {
                    "apiVersion": TRAEFIK_API_VERSION,
                    "kind": "Middleware",
                    "metadata": {
                        "name": object_name(site.pk, f"{suffix}-strip"),
                        "namespace": namespace,
                        "labels": site_labels(site.pk, "route"),
                    },
                    "spec": {"stripPrefix": {"prefixes": [path_prefix.rstrip("/")]}},
                }
            )
    return result


def ingress_route(site: SiteInfo, *, through_gateway: bool) -> dict[str, Any]:
    backend = object_name(site.pk, "-proxy" if through_gateway else "")
    routes = []
    for suffix, rule, path_prefix in route_rules(site):
        used = [{"name": object_name(site.pk, "-body")}]
        if path_prefix != "/":
            used.append({"name": object_name(site.pk, f"{suffix}-strip")})
        routes.append(
            {
                "kind": "Rule",
                "match": rule,
                "middlewares": used,
                "services": [{"name": backend, "port": 80}],
            }
        )
    return {
        "apiVersion": TRAEFIK_API_VERSION,
        "kind": "IngressRoute",
        "metadata": {
            "name": object_name(site.pk),
            "namespace": settings.KUBERNETES_NAMESPACE,
            "labels": site_labels(site.pk, "route"),
        },
        "spec": {
            "entryPoints": [
                entrypoint.strip()
                for entrypoint in settings.KUBERNETES_TRAEFIK_ENTRYPOINTS.split(",")
                if entrypoint.strip()
            ],
            "routes": routes,
        },
    }


def terminal_pod(  # noqa: PLR0913 -- every value is a distinct part of the pod spec.
    site_id: int,
    name: str,
    image: str,
    command: list[str],
    environment: dict[str, str],
    *,
    host_dir: str,
    uid: int,
    gid: int,
    memory: int,
    nano_cpus: int,
) -> dict[str, Any]:
    """A disposable TTY pod whose only persistent mount is this site's directory."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": settings.KUBERNETES_NAMESPACE,
            "labels": site_labels(site_id, "terminal"),
        },
        "spec": {
            **pod_defaults(),
            "restartPolicy": "Never",
            # Terminal sessions end after an hour; this is a backstop if cleanup is missed.
            "activeDeadlineSeconds": 3900,
            "terminationGracePeriodSeconds": 1,
            "securityContext": {"runAsUser": uid, "runAsGroup": gid, "runAsNonRoot": uid != 0},
            "containers": [
                {
                    "name": "terminal",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": command,
                    "env": [
                        {"name": key, "value": value}
                        for key, value in {
                            "TERM": "xterm-256color",
                            "HOME": "/tmp",
                            **environment,
                        }.items()
                    ],
                    "workingDir": "/site",
                    "stdin": True,
                    "stdinOnce": True,
                    "tty": True,
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "limits": {"cpu": cpu_quantity(nano_cpus), "memory": str(memory)},
                        "requests": {"cpu": "10m", "memory": str(min(memory, 32 * 1024**2))},
                    },
                    "volumeMounts": [
                        {"name": "site", "mountPath": "/site"},
                        {"name": "tmp", "mountPath": "/tmp"},
                        {"name": "run", "mountPath": "/run"},
                    ],
                }
            ],
            "volumes": [
                {"name": "site", "hostPath": {"path": host_dir, "type": "Directory"}},
                memory_volume("tmp", 64 * 1024**2),
                memory_volume("run", 8 * 1024**2),
            ],
        },
    }
