import random
from collections.abc import Iterator

import requests

from .appserver import Appserver
from .models import Site
from .operations import UserFacingError


def raise_by_recoverability(site: Site, response: requests.Response):
    if response.status_code == 200:
        return
    if response.status_code == 422:
        raise RuntimeError(f"Invalid JSON: {response.json()}")
    try:
        content = response.json()
    except requests.exceptions.JSONDecodeError as e:
        raise ValueError(
            f"Appserver ({site=}) returned {response.status_code} and could not be decoded to JSON."
        ) from e

    # FastAPI's HTTPException wraps its structured payload in a "detail" object.
    if isinstance(content, dict) and isinstance(content.get("detail"), dict):
        content = content["detail"]

    if (
        isinstance(content, dict)
        and content.get("user_error")
        and (explanation := content.get("explanation"))
    ):
        description = content.get("description", "An error occurred")
        raise UserFacingError(f"{description}: {explanation}")
    response.raise_for_status()


def update_docker_service(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    """Create or update a Docker service for a site.

    Expects scope to be populated with ``pingable_appservers``.
    If scope has a :class:`.SiteConfig`, it will use the Docker base image from there.
    """
    if site.availability == "disabled":
        yield from remove_docker_service(site, appservers)
        return
    appserver = random.choice(appservers)
    yield f"Connecting to {appserver} to create/update docker service."

    response = appserver.http_request(
        "/api/docker/service/update",
        method="POST",
        data=site.serialize_for_appserver(),
    )
    raise_by_recoverability(site, response)
    yield "Created/updated Docker service"


def build_docker_image(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    from .images import image_setup_for_site

    appserver = random.choice(appservers)
    yield f"Connecting to appserver {appserver} to build docker image."
    data = {"site": site.serialize_for_appserver()}
    image_setup = image_setup_for_site(site)
    if image_setup is not None:
        data["site"]["image_setup"] = image_setup
    response = appserver.http_request(
        "/api/docker/image/build",
        method="POST",
        data=data,
    )
    raise_by_recoverability(site, response)
    yield "Docker image built"


def delete_site_files(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    appserver = random.choice(appservers)
    yield f"Connecting to {appserver} to delete site files."
    response = appserver.http_request(
        "/api/files/delete-all",
        method="POST",
        data=site.serialize_for_appserver(),
    )
    raise_by_recoverability(site, response)
    yield "Site files deleted"


def delete_site_database(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    from .database_actions import delete_site_database as delete_database_resources

    yield from delete_database_resources(site, appservers)


def remove_docker_service(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    appserver = random.choice(appservers)
    yield f"Removing Docker service on {appserver}"
    response = appserver.http_request(
        "/api/docker/service/remove",
        method="POST",
        data=site.serialize_for_appserver(),
    )
    raise_by_recoverability(site, response)
    yield "Docker service removed"


def remove_docker_image(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    appserver = random.choice(appservers)
    yield f"Removing Docker image on {appserver}"
    response = appserver.http_request(
        "/api/docker/image/delete",
        method="POST",
        data=site.serialize_for_appserver(),
    )
    raise_by_recoverability(site, response)
    yield "Docker image removed"
