from typing import Any, cast
from unittest.mock import Mock

import docker.errors
import pytest
from fastapi import HTTPException

from orchestrator.api.docker import router
from orchestrator.api.docker.schema import SiteInfo


def test_service_update_restarts_existing_process(site_info: SiteInfo, monkeypatch):
    client = Mock()
    service = Mock()
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    monkeypatch.setattr(router.services, "find_service_by_name", lambda *_args: service)

    assert router.update_docker_service(site_info) == {}
    assert service.update.call_args.kwargs["force_update"] is True
    assert service.update.call_args.kwargs["name"] == str(site_info)
    client.services.create.assert_not_called()


def test_first_deployment_creates_service(site_info: SiteInfo, monkeypatch):
    client = Mock()
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    monkeypatch.setattr(router.services, "find_service_by_name", lambda *_args: None)

    assert router.update_docker_service(site_info) == {}
    assert client.services.create.call_args.kwargs["name"] == str(site_info)
    assert "force_update" not in client.services.create.call_args.kwargs


def test_build_image_creates_default_dockerfile(site_info: SiteInfo, monkeypatch):
    client = Mock()
    client.images.build.return_value = (Mock(), iter([{"stream": "Built image"}]))
    monkeypatch.setattr(router.docker, "from_env", lambda: client)

    assert router.build_image(site_info) == {"build_stdout": ({"stream": "Built image"},)}
    assert (site_info.directory_path() / "Dockerfile").read_text() == "FROM alpine:latest\n"
    assert client.images.build.call_args.kwargs["tag"] == str(site_info)


def test_build_image_preserves_custom_dockerfile(site_info: SiteInfo, monkeypatch):
    client = Mock()
    client.images.build.return_value = (Mock(), iter([]))
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    dockerfile = site_info.directory_path() / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-alpine\n")

    router.build_image(site_info)
    assert dockerfile.read_text() == "FROM python:3.13-alpine\n"


def test_build_error_reports_user_failure(site_info: SiteInfo, monkeypatch):
    client = Mock()
    client.images.build.side_effect = docker.errors.BuildError(
        "Invalid Dockerfile", [{"error": "Invalid Dockerfile"}]
    )
    monkeypatch.setattr(router.docker, "from_env", lambda: client)

    with pytest.raises(HTTPException) as exc_info:
        router.build_image(site_info)
    assert exc_info.value.status_code == 500
    detail = cast(dict[str, Any], exc_info.value.detail)
    assert detail["user_error"] is True
    assert detail["explanation"] == "Invalid Dockerfile"


def test_service_removal_reports_docker_failure(site_info: SiteInfo, monkeypatch):
    client = Mock()
    service = Mock()
    service.remove.side_effect = docker.errors.APIError("Docker unavailable")
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    monkeypatch.setattr(router.services, "find_service_by_name", lambda *_args: service)

    with pytest.raises(docker.errors.APIError, match="Docker unavailable"):
        router.remove_docker_service(site_info)


def test_service_removal_tolerates_already_removed_service(site_info: SiteInfo, monkeypatch):
    client = Mock()
    service = Mock()
    service.remove.side_effect = docker.errors.NotFound("Service not found")
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    monkeypatch.setattr(router.services, "find_service_by_name", lambda *_args: service)

    assert router.remove_docker_service(site_info) == {}


def test_service_removal_tolerates_missing_service(site_info: SiteInfo, monkeypatch):
    client = Mock()
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    monkeypatch.setattr(router.services, "find_service_by_name", lambda *_args: None)

    assert router.remove_docker_service(site_info) == {}


def test_image_removal_reports_docker_failure(site_info: SiteInfo, monkeypatch):
    client = Mock()
    client.images.remove.side_effect = docker.errors.APIError("Image is in use")
    monkeypatch.setattr(router.docker, "from_env", lambda: client)

    with pytest.raises(docker.errors.APIError, match="Image is in use"):
        router.delete_image(site_info)


@pytest.mark.parametrize("not_found", (docker.errors.NotFound, docker.errors.ImageNotFound))
def test_image_removal_tolerates_missing_image(site_info: SiteInfo, monkeypatch, not_found):
    client = Mock()
    client.images.remove.side_effect = not_found("Image not found")
    monkeypatch.setattr(router.docker, "from_env", lambda: client)

    assert router.delete_image(site_info) == {}
