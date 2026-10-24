"""Deploy sites to Kubernetes.

Images are still built with Docker, then pushed to a registry the cluster pulls
from, and pods reference them by digest.
"""

import contextlib
from datetime import UTC, datetime

import docker
import docker.errors

from orchestrator import settings

from ..docker import gateway
from ..docker.schema import SiteInfo
from ..docker.services import find_service_by_name, prepare_site_directory
from . import cluster, manifests


class ImageUnavailable(Exception):  # noqa: N818
    """The site has no pushed image to run."""


def registry_repository(site_name: str) -> str:
    return f"{settings.KUBERNETES_IMAGE_REGISTRY}/{site_name}"


def push_image(client: docker.DockerClient, site: SiteInfo) -> None:
    """Publish the freshly built ``site_0001`` image for the cluster to pull."""
    repository = registry_repository(str(site))
    client.images.get(str(site)).tag(repository, "latest")
    for line in client.images.push(repository, tag="latest", stream=True, decode=True):
        if "error" in line:
            raise docker.errors.APIError(line["error"])


def pushed_image(client: docker.DockerClient, site_name: str) -> str | None:
    """The digest reference of the last pushed image, so every deploy pins an exact build."""
    repository = registry_repository(site_name)
    try:
        image = client.images.get(f"{repository}:latest")
    except docker.errors.ImageNotFound:
        return None
    for digest in image.attrs.get("RepoDigests", []):
        if digest.startswith(f"{repository}@"):
            return digest
    return None


def remove_image(client: docker.DockerClient, site: SiteInfo) -> None:
    with contextlib.suppress(docker.errors.NotFound):
        client.images.remove(f"{registry_repository(str(site))}:latest")


def remove_swarm_service(client: docker.DockerClient, site: SiteInfo) -> None:
    """Retire the site's Swarm service once Kubernetes serves it, so routes do not overlap."""
    for name in (str(site), f"{site}-proxy"):
        try:
            service = find_service_by_name(client, name)
        except docker.errors.APIError:
            # This Docker engine is not a Swarm manager, so there is nothing to retire.
            return
        if service is not None:
            with contextlib.suppress(docker.errors.NotFound):
                service.remove()


def deploy_site(client: docker.DockerClient, site: SiteInfo) -> None:
    prepare_site_directory(site)
    image = settings.STATIC_SITE_IMAGE
    if site.type_ == "dynamic":
        pushed = pushed_image(client, str(site))
        if pushed is None:
            raise ImageUnavailable("Build the site's image before deploying it.")
        image = pushed

    # Validate and publish an administrator's Nginx rules exactly as for Swarm.
    custom = gateway.prepare_gateway(client, site) is not None
    config = str(gateway.config_path(site, on_host=True)) if custom else None
    restarted_at = datetime.now(UTC).isoformat()

    through_gateway = custom and site.type_ == "dynamic"
    objects = [
        manifests.site_deployment(
            site, image, restarted_at, config if site.type_ == "static" else None
        ),
        manifests.service(site.pk),
    ]
    if through_gateway:
        assert config is not None
        objects += [
            manifests.gateway_deployment(site, config, restarted_at),
            manifests.service(site.pk, "proxy"),
        ]
    objects += [
        *manifests.middlewares(site),
        manifests.ingress_route(site, through_gateway=through_gateway),
    ]
    cluster.apply_site(objects, site.pk)
    remove_swarm_service(client, site)


def remove_site(client: docker.DockerClient, site: SiteInfo) -> None:
    cluster.remove_site_objects(site.pk)
    remove_swarm_service(client, site)
    gateway.remove_config(site)
