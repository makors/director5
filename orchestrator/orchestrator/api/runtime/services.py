"""Inspect site services and run disposable, site-confined workspace terminals."""

import contextlib
import os
import socket
from types import SimpleNamespace

import docker
from pydantic import BaseModel, Field, field_validator, model_validator

from orchestrator import settings

from ..docker.schema import ResourceLimits
from ..docker.services import find_service_by_name
from ..files.storage import SiteFiles

MAX_LOG_BYTES = 64 * 1024
MAX_TERMINAL_INPUT = 8192
DATABASE_ENVIRONMENT = {"PGPASSWORD", "MYSQL_PWD"}
WORKSPACE_ENVIRONMENT = {
    "DATABASE_URL",
    "DIRECTOR_DATABASE_URL",
    "DIRECTOR_DATABASE_TYPE",
    "DIRECTOR_DATABASE_HOST",
    "DIRECTOR_DATABASE_PORT",
    "DIRECTOR_DATABASE_NAME",
    "DIRECTOR_DATABASE_USERNAME",
    "DIRECTOR_DATABASE_PASSWORD",
    "TZ",
}


class RuntimeUnavailable(Exception):  # noqa: N818
    """The site's own runtime is absent or cannot be reached."""


class TerminalOptions(BaseModel):
    kind: str = "shell"
    command: list[str] = Field(default_factory=list, max_length=16)
    environment: dict[str, str] = Field(default_factory=dict)
    workspace_image: str | None = Field(
        default=None, min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/@-]*$"
    )
    resource_limits: ResourceLimits | None = None

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value):
        if value not in {"shell", "database"}:
            raise ValueError("Unknown terminal kind")
        return value

    @field_validator("command")
    @classmethod
    def bounded_command(cls, value):
        if any(len(part) > 1024 or "\x00" in part for part in value):
            raise ValueError("Invalid terminal command")
        return value

    @field_validator("environment")
    @classmethod
    def bounded_environment(cls, value):
        if set(value) - (DATABASE_ENVIRONMENT | WORKSPACE_ENVIRONMENT) or any(
            len(secret) > 4096 or "\x00" in secret for secret in value.values()
        ):
            raise ValueError("Invalid database environment")
        return value

    @model_validator(mode="after")
    def environment_matches_kind(self):
        allowed = DATABASE_ENVIRONMENT if self.kind == "database" else WORKSPACE_ENVIRONMENT
        if set(self.environment) - allowed:
            raise ValueError("Environment does not match terminal kind")
        return self


def site_service(client, site_id):
    if site_id < 1:
        raise RuntimeUnavailable("Site not found.")
    service = find_service_by_name(client, f"site_{site_id:04d}")
    if service is None:
        raise RuntimeUnavailable("This site has no deployed service.")
    return service


def runtime_status(client, site_id):
    service = site_service(client, site_id)
    tasks = service.tasks()
    active = [task for task in tasks if task.get("DesiredState") == "running"]
    running = sum(task.get("Status", {}).get("State") == "running" for task in active)
    desired = service.attrs.get("Spec", {}).get("Mode", {}).get("Replicated", {}).get("Replicas", 0)
    state = "running" if running else "starting" if desired else "stopped"
    if running and running < desired:
        state = "degraded"
    return {"state": state, "running": running, "desired": desired}


def runtime_logs(client, site_id, tail=200):
    service = site_service(client, site_id)
    stream = service.logs(stdout=True, stderr=True, timestamps=True, tail=tail, follow=False)
    output = bytearray()
    truncated = False
    try:
        for chunk in stream:
            remaining = MAX_LOG_BYTES - len(output)
            output.extend(chunk[:remaining])
            if len(chunk) > remaining or len(output) >= MAX_LOG_BYTES:
                truncated = True
                break
    finally:
        if hasattr(stream, "close"):
            stream.close()
    return {"output": output.decode("utf-8", errors="replace"), "truncated": truncated}


def workspace_image(client, site_id, options):
    """Prefer this site's built image, then its Manager-approved base image."""
    image = f"site_{site_id:04d}"
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        return options.workspace_image or getattr(
            settings, "RUNTIME_WORKSPACE_IMAGE", "alpine:3.21"
        )
    return image


def create_helper(client, site_id, options, command, environment):
    """Create a disposable TTY whose only persistent mount belongs to this site."""
    if site_id < 1:
        raise RuntimeUnavailable("Site not found.")
    network = getattr(settings, "RUNTIME_HELPER_NETWORK", "bridge")
    if network == "host" or network.startswith("container:"):
        raise RuntimeUnavailable("Terminal helpers require an isolated Docker network.")
    if options.kind == "database":
        setting, default = (
            ("RUNTIME_POSTGRES_IMAGE", "postgres:17-alpine")
            if command[0] == "psql"
            else ("RUNTIME_MYSQL_IMAGE", "mysql:8.4")
        )
        image = getattr(settings, setting, default)
    else:
        image = workspace_image(client, site_id, options)
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        client.images.pull(image)
    limits = options.resource_limits
    memory = min(limits.memory, 2 * 1024**3) if limits else 256 * 1024**2
    cpus = min(limits.cpus, 2 * 10**9) if limits else 500_000_000
    host_root = settings.HOST_SITES_DIR / f"{site_id // 100:02d}" / f"{site_id % 100:02d}"
    # SiteFiles opens each local path component with O_NOFOLLOW before Docker mounts
    # the corresponding host path. A sibling site's directory cannot be substituted.
    with SiteFiles(SimpleNamespace(pk=site_id)) as files:
        owner = os.fstat(files.root_fd)
        return client.containers.create(
            image=image,
            command=command,
            entrypoint=[],
            environment={"TERM": "xterm-256color", "HOME": "/tmp", **environment},
            working_dir="/site",
            user=f"{owner.st_uid}:{owner.st_gid}",
            tty=True,
            stdin_open=True,
            auto_remove=True,
            labels={"director.runtime.site": str(site_id), "director.runtime.helper": "true"},
            mounts=[docker.types.Mount(target="/site", source=str(host_root), type="bind")],
            tmpfs={"/tmp": "rw,nosuid,nodev,size=64m", "/run": "rw,nosuid,nodev,size=8m"},
            read_only=True,
            privileged=False,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit=max(memory, 32 * 1024**2),
            memswap_limit=max(memory, 32 * 1024**2),
            nano_cpus=max(cpus, 10_000_000),
            pids_limit=128,
            network_mode=network,
            init=True,
        )


class TerminalSession:
    """A disposable Docker TTY whose complete process tree ends on close."""

    def __init__(self, client, site_id, options):
        self.client = client
        self.attachment = None
        self.socket = None
        self.container = None
        if options.kind == "database":
            if not options.command or options.command[0] not in {"psql", "mysql"}:
                raise RuntimeUnavailable("A database client is required for this terminal.")
            command = options.command
            environment = options.environment
        else:
            if options.command:
                raise RuntimeUnavailable("Shell options cannot replace the site's shell.")
            command = ["/bin/sh", "-c", "stty sane 2>/dev/null; exec /bin/sh -i"]
            environment = options.environment
        try:
            self.container = create_helper(client, site_id, options, command, environment)
            self.attachment = self.container.attach_socket(
                params={"stdin": True, "stdout": True, "stderr": True, "stream": True}
            )
            self.container.start()
            self.socket = getattr(self.attachment, "_sock", self.attachment)
            self.socket.settimeout(0.25)
            self.resize(24, 80)
        except Exception:
            self.close()
            raise

    def read(self):
        try:
            return self.socket.recv(4096)
        except TimeoutError:
            return None

    def write(self, data):
        if len(data) > MAX_TERMINAL_INPUT:
            raise ValueError("Terminal input is too large")
        self.socket.sendall(data)

    def resize(self, rows, cols):
        if not 2 <= rows <= 120 or not 10 <= cols <= 300:
            raise ValueError("Invalid terminal size")
        self.container.resize(height=rows, width=cols)

    def close(self):
        if self.socket is not None:
            with contextlib.suppress(OSError):
                self.socket.shutdown(socket.SHUT_RDWR)
        if self.attachment is not None:
            # The SDK retains the HTTP response on SocketIO. Close the response
            # before its raw stream so urllib3 cannot later flush a closed file.
            response = getattr(self.attachment, "_response", None)
            if response is not None:
                with contextlib.suppress(OSError, ValueError):
                    response.close()
            with contextlib.suppress(OSError):
                self.attachment.close()
        if self.socket is not None:
            with contextlib.suppress(OSError):
                self.socket.close()
        if self.container is not None:
            try:
                self.container.remove(force=True, v=True)
            except docker.errors.NotFound:
                pass
            except docker.errors.APIError as error:
                # auto_remove may already be removing the process after stdin
                # closes. A 409 for that exact state still means cleanup won.
                if error.status_code != 409 or "removal" not in str(error).lower():
                    raise
            self.container = None


def docker_client():
    return docker.from_env(timeout=10)
