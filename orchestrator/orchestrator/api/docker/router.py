import contextlib
import json
import shlex
import traceback
from pathlib import Path
from typing import Any

import docker
import docker.errors
from fastapi import APIRouter, HTTPException

from orchestrator.api.files.storage import FileOperationError, SiteFiles, file_error

from . import gateway, services
from .build_context import build_context
from .schema import ContainerLimits, ExceptionInfo, SiteInfo

router = APIRouter()

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _build_dockerfile(site: SiteInfo, files: SiteFiles) -> str:
    """Keep a user Dockerfile intact when building an approved catalog image."""
    setup = site.image_setup
    if setup is None:
        try:
            return files.read("Dockerfile")["content"]
        except FileNotFoundError:
            default = (TEMPLATE_DIR / "Dockerfile").read_text()
            files.write("Dockerfile", default.encode("utf-8"), create_only=True)
            return default
    if setup.run_script_template is not None:
        files.write("run.sh", setup.run_script_template.encode("utf-8"), mode="755")
    lines = [f"FROM {setup.base_image}"]
    commands = [command for command in setup.setup_commands if command.strip()]
    if setup.packages:
        commands.append(f"{setup.install_command_prefix} {shlex.join(setup.packages)}")
    lines.extend("RUN " + json.dumps(["/bin/sh", "-c", command]) for command in commands)
    return "\n".join(lines) + "\n"


@router.post(
    "/image/build",
    responses={
        "500": {"model": ExceptionInfo},
    },
)
def build_image(
    site: SiteInfo,
    resource_limits: ContainerLimits | None = None,
) -> dict[str, Any]:
    client = docker.from_env()

    # caching or storing intermediate images takes up a
    # ton of space.
    try:
        with SiteFiles(site) as files:
            dockerfile = _build_dockerfile(site, files)
            with build_context(files, dockerfile) as context:
                _image, log = client.images.build(
                    fileobj=context,
                    custom_context=True,
                    dockerfile="Dockerfile",
                    rm=True,
                    container_limits=resource_limits,  # type: ignore[assignment]
                    tag=str(site),
                )
    except (FileOperationError, OSError) as exc:
        error = file_error(exc) if isinstance(exc, OSError) else exc
        raise HTTPException(
            status_code=400,
            detail={
                "user_error": True,
                "description": "Invalid build files",
                "explanation": str(error),
            },
        ) from exc
    except docker.errors.BuildError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "user_error": True,
                "description": "Failed to build image",
                "explanation": e.msg,
                "traceback": tuple(e.build_log),
            },
        ) from e
    except docker.errors.APIError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "user_error": True,
                "description": "Failed to build image",
                "explanation": e.explanation,
            },
        ) from e
    return {"build_stdout": tuple(log)}


@router.post("/image/delete")
def delete_image(site: SiteInfo):
    client = docker.from_env()
    with contextlib.suppress(docker.errors.NotFound):
        client.images.remove(str(site))
    return {}


@router.post("/service/update")
def update_docker_service(site_info: SiteInfo):
    """Creates, or updates the Docker service running the site.

    Note that this expects that a docker image exists with the correct tag.
    """
    params = services.create_service_params(site_info)
    client = docker.from_env()
    try:
        custom_mount = gateway.prepare_gateway(client, site_info)
    except docker.errors.DockerException as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "user_error": True,
                "description": "Invalid Nginx configuration",
                "explanation": "The configuration could not be applied. Check its syntax and retry.",
            },
        ) from exc
    if custom_mount is not None and site_info.type_ == "static":
        params["mounts"].append(custom_mount)
    if custom_mount is not None and site_info.type_ == "dynamic":
        params["labels"] = {}
    service = services.find_service_by_name(client, str(site_info))
    try:
        if service is None:
            client.services.create(**params)
        else:
            # Updating an unchanged spec otherwise leaves the existing process running.
            service.update(**params, force_update=True)
        proxy = services.find_service_by_name(client, f"{site_info}-proxy")
        if custom_mount is not None and site_info.type_ == "dynamic":
            proxy_params = gateway.gateway_params(site_info, custom_mount)
            if proxy is None:
                client.services.create(**proxy_params)
            else:
                proxy.update(**proxy_params, force_update=True)
        elif proxy is not None:
            proxy.remove()
    except docker.errors.APIError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "description": "Failed to update service",
                "traceback": traceback.format_exc(),
            },
        ) from e
    return {}


@router.post("/service/remove")
def remove_docker_service(site: SiteInfo):
    client = docker.from_env()
    service = services.find_service_by_name(client, str(site))
    if service is not None:
        with contextlib.suppress(docker.errors.NotFound):
            service.remove()
    proxy = services.find_service_by_name(client, f"{site}-proxy")
    if proxy is not None:
        with contextlib.suppress(docker.errors.NotFound):
            proxy.remove()
    gateway.remove_config(site)
    return {}
