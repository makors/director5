"""Site status, logs and disposable terminal pods on Kubernetes."""

import contextlib
import json
import os
import secrets
import time
from types import SimpleNamespace
from typing import Any, cast

from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream
from kubernetes.stream.ws_client import RESIZE_CHANNEL

from orchestrator import settings

from ..docker.schema import SiteInfo
from ..files.storage import SiteFiles
from ..runtime.services import MAX_LOG_BYTES, MAX_TERMINAL_INPUT, RuntimeUnavailable
from . import cluster, manifests, sites


def _deployment(site_id: int) -> Any:
    if site_id < 1:
        raise RuntimeUnavailable("Site not found.")
    try:
        return cluster.apps().read_namespaced_deployment(
            manifests.object_name(site_id), settings.KUBERNETES_NAMESPACE
        )
    except ApiException as error:
        if cluster.missing(error):
            raise RuntimeUnavailable("This site has no deployed service.") from error
        raise


def _site_pods(site_id: int) -> list[Any]:
    labels = ",".join(
        f"{key}={value}" for key, value in manifests.selector(site_id, "site").items()
    )
    pods = (
        cluster.core()
        .list_namespaced_pod(settings.KUBERNETES_NAMESPACE, label_selector=labels)
        .items
    )
    return sorted(pods, key=lambda pod: pod.metadata.creation_timestamp)


def _running(pod: Any) -> bool:
    statuses = pod.status.container_statuses or []
    return (
        pod.metadata.deletion_timestamp is None
        and pod.status.phase == "Running"
        and any(status.state.running is not None for status in statuses)
    )


def runtime_status(_client: Any, site_id: int) -> dict[str, Any]:
    desired = _deployment(site_id).spec.replicas or 0
    running = sum(_running(pod) for pod in _site_pods(site_id))
    state = "running" if running else "starting" if desired else "stopped"
    if running and running < desired:
        state = "degraded"
    return {"state": state, "running": running, "desired": desired}


def runtime_logs(_client: Any, site_id: int, tail: int = 200) -> dict[str, Any]:
    _deployment(site_id)
    pods = _site_pods(site_id)
    if not pods:
        return {"output": "", "truncated": False}
    try:
        output = cluster.core().read_namespaced_pod_log(
            pods[-1].metadata.name,
            settings.KUBERNETES_NAMESPACE,
            container="site",
            tail_lines=tail,
            timestamps=True,
            limit_bytes=MAX_LOG_BYTES + 1,
        )
    except ApiException as error:
        # A pod that has not started its container yet has no logs to read.
        if error.status == 400:
            return {"output": "", "truncated": False}
        raise
    encoded = output.encode("utf-8")
    truncated = len(encoded) > MAX_LOG_BYTES
    return {
        "output": encoded[:MAX_LOG_BYTES].decode("utf-8", errors="replace"),
        "truncated": truncated,
    }


def terminal_image(docker_client: Any, site_id: int, options: Any, command: list[str]) -> str:
    if options.kind == "database":
        if command[0] == "psql":
            return settings.RUNTIME_POSTGRES_IMAGE
        return settings.RUNTIME_MYSQL_IMAGE
    # Prefer this site's built image, then its Manager-approved base image.
    return (
        sites.pushed_image(docker_client, f"site_{site_id:04d}")
        or options.workspace_image
        or settings.RUNTIME_WORKSPACE_IMAGE
    )


def _wait_until_running(name: str) -> None:
    deadline = time.monotonic() + settings.KUBERNETES_POD_START_TIMEOUT
    api = cluster.core()
    while time.monotonic() < deadline:
        pod = api.read_namespaced_pod(name, settings.KUBERNETES_NAMESPACE)
        if pod.status.phase == "Running":
            return
        if pod.status.phase in {"Succeeded", "Failed"}:
            raise RuntimeUnavailable("The terminal stopped before it could start.")
        for status in pod.status.container_statuses or []:
            waiting = status.state.waiting
            if waiting is not None and waiting.reason in {
                "ErrImagePull",
                "ImagePullBackOff",
                "InvalidImageName",
                "CreateContainerError",
            }:
                raise RuntimeUnavailable("The terminal image could not be started.")
        time.sleep(0.5)
    raise RuntimeUnavailable("The terminal did not start in time.")


class TerminalSession:
    """A disposable terminal pod whose complete process tree ends on close."""

    def __init__(self, docker_client: Any, site_id: int, options: Any) -> None:
        if site_id < 1:
            raise RuntimeUnavailable("Site not found.")
        self.name: str | None = None
        self.connection: Any = None
        if options.kind == "database":
            if not options.command or options.command[0] not in {"psql", "mysql"}:
                raise RuntimeUnavailable("A database client is required for this terminal.")
            command = options.command
        else:
            if options.command:
                raise RuntimeUnavailable("Shell options cannot replace the site's shell.")
            command = ["/bin/sh", "-c", "stty sane 2>/dev/null; exec /bin/sh -i"]
        limits = options.resource_limits
        memory = min(limits.memory, 2 * 1024**3) if limits else 256 * 1024**2
        cpus = min(limits.cpus, 2 * 10**9) if limits else 500_000_000
        host_root = settings.HOST_SITES_DIR / f"{site_id // 100:02d}" / f"{site_id % 100:02d}"
        try:
            # SiteFiles opens each path component with O_NOFOLLOW, so a sibling site's
            # directory cannot be substituted for this one before the pod mounts it.
            with SiteFiles(cast(SiteInfo, SimpleNamespace(pk=site_id))) as files:
                owner = os.fstat(files.root_fd)
                self.name = manifests.object_name(site_id, f"-term-{secrets.token_hex(4)}")
                body = manifests.terminal_pod(
                    site_id,
                    self.name,
                    terminal_image(docker_client, site_id, options, command),
                    command,
                    options.environment,
                    host_dir=str(host_root),
                    uid=owner.st_uid,
                    gid=owner.st_gid,
                    memory=max(memory, 32 * 1024**2),
                    nano_cpus=max(cpus, 10_000_000),
                )
                cluster.core().create_namespaced_pod(settings.KUBERNETES_NAMESPACE, body)
            _wait_until_running(self.name)
            self.connection = stream(
                cluster.core().connect_get_namespaced_pod_attach,
                self.name,
                settings.KUBERNETES_NAMESPACE,
                container="terminal",
                stdin=True,
                stdout=True,
                stderr=True,
                tty=True,
                binary=True,
                _preload_content=False,
            )
            self.resize(24, 80)
        except Exception:
            self.close()
            raise

    def read(self) -> bytes | None:
        if not self.connection.is_open():
            return b""
        self.connection.update(timeout=0.25)
        data = self.connection.read_stdout() + self.connection.read_stderr()
        if data:
            return data
        return None if self.connection.is_open() else b""

    def write(self, data: bytes) -> None:
        if len(data) > MAX_TERMINAL_INPUT:
            raise ValueError("Terminal input is too large")
        self.connection.write_stdin(data)

    def resize(self, rows: int, cols: int) -> None:
        if not 2 <= rows <= 120 or not 10 <= cols <= 300:
            raise ValueError("Invalid terminal size")
        self.connection.write_channel(
            RESIZE_CHANNEL, json.dumps({"Height": rows, "Width": cols}).encode()
        )

    def close(self) -> None:
        if self.connection is not None:
            with contextlib.suppress(Exception):
                self.connection.close()
            self.connection = None
        if self.name is not None:
            with contextlib.suppress(ApiException):
                cluster.core().delete_namespaced_pod(
                    self.name, settings.KUBERNETES_NAMESPACE, grace_period_seconds=0
                )
            self.name = None
