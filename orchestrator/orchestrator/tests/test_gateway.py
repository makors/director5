"""Custom Nginx config validation, publication, routing, and safe failure behavior."""

import os
from pathlib import Path
from unittest.mock import Mock

import docker
import docker.errors
import pytest
from fastapi import HTTPException

from orchestrator import settings
from orchestrator.api.docker import gateway, router


@pytest.fixture(autouse=True)
def gateway_roots(tmp_path, monkeypatch, isolate_site_directories):
    monkeypatch.setattr(settings, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(settings, "HOST_SITES_DIR", tmp_path / "sites")


@pytest.mark.parametrize("mode", ("static", "dynamic"))
def test_gateway_validates_candidate_before_publishing(site_info, mode):
    site_info.type_ = mode
    site_info.custom_nginx_config = 'add_header X-Director-Test "ok" always;'
    target = gateway.config_path(site_info)
    target.parent.mkdir(parents=True)
    target.write_text("previous valid configuration")
    client = Mock()

    def validate(image, command, **kwargs):
        assert image == "nginx:latest"
        assert command == ["nginx", "-t"]
        assert kwargs["network_mode"] == "none"
        assert kwargs["remove"] is True
        assert target.read_text() == "previous valid configuration"
        mount = kwargs["mounts"][0]
        assert mount["ReadOnly"] is True
        candidate = Path(mount["Source"])
        assert candidate != target
        text = candidate.read_text()
        assert site_info.custom_nginx_config in text
        assert f"client_max_body_size {site_info.resource_limits.max_request_body_size};" in text
        if mode == "static":
            assert "root /usr/share/nginx/html;" in text
            assert "try_files $uri $uri/ =404;" in text
        else:
            assert f"set $director_backend http://{site_info}:80;" in text
            assert "proxy_pass $director_backend;" in text

    client.containers.run.side_effect = validate
    mount = gateway.prepare_gateway(client, site_info)
    assert mount["Source"] == str(target)
    assert mount["ReadOnly"] is True
    assert site_info.custom_nginx_config in target.read_text()
    assert list(target.parent.iterdir()) == [target]
    assert not target.is_relative_to(site_info.directory_path())


def test_invalid_config_retains_previous_file_and_removes_candidate(site_info):
    site_info.custom_nginx_config = "unknown_directive on;"
    target = gateway.config_path(site_info)
    target.parent.mkdir(parents=True)
    target.write_text("previous working configuration")
    client = Mock()
    client.containers.run.side_effect = docker.errors.ContainerError(
        Mock(), 1, "nginx -t", "nginx:latest", "invalid directive"
    )
    with pytest.raises(docker.errors.ContainerError):
        gateway.prepare_gateway(client, site_info)
    assert target.read_text() == "previous working configuration"
    assert list(target.parent.iterdir()) == [target]


def test_host_mount_maps_candidate_and_final_name(site_info, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "HOST_SITES_DIR", tmp_path / "host" / "sites")
    site_info.custom_nginx_config = "add_header X-Test yes;"
    client = Mock()
    mount = gateway.prepare_gateway(client, site_info)
    candidate = client.containers.run.call_args.kwargs["mounts"][0]
    assert Path(candidate["Source"]).parent == gateway.config_path(site_info, on_host=True).parent
    assert mount["Source"] == str(gateway.config_path(site_info, on_host=True))
    assert not Path(candidate["Source"]).name.endswith(".conf")


def test_empty_custom_config_needs_no_validation_container(site_info):
    client = Mock()
    assert gateway.prepare_gateway(client, site_info) is None
    client.containers.run.assert_not_called()


def test_static_service_mounts_config_and_keeps_direct_routes(site_info, monkeypatch):
    site_info.custom_nginx_config = "add_header X-Test yes;"
    client = Mock()
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    monkeypatch.setattr(router.services, "find_service_by_name", lambda *_args: None)
    assert router.update_docker_service(site_info) == {}
    params = client.services.create.call_args.kwargs
    assert params["name"] == str(site_info)
    assert params["image"] == "nginx:latest"
    assert any(
        mount["Target"] == "/etc/nginx/conf.d/default.conf" and mount["ReadOnly"]
        for mount in params["mounts"]
    )
    assert params["labels"][f"traefik.http.services.{site_info}.loadbalancer.server.port"] == "80"
    client.containers.run.assert_called_once()


def test_dynamic_custom_config_moves_routes_to_proxy_and_forces_both_updates(
    site_info, monkeypatch
):
    site_info.type_ = "dynamic"
    site_info.custom_nginx_config = "add_header X-Test yes;"
    client, backend, proxy = Mock(), Mock(), Mock()
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    monkeypatch.setattr(
        router.services,
        "find_service_by_name",
        lambda _client, name: proxy if name.endswith("-proxy") else backend,
    )
    assert router.update_docker_service(site_info) == {}
    assert backend.update.call_args.kwargs["labels"] == {}
    assert backend.update.call_args.kwargs["force_update"] is True
    params = proxy.update.call_args.kwargs
    assert params["force_update"] is True
    assert params["name"] == f"{site_info}-proxy"
    assert params["labels"][f"traefik.http.services.{site_info}.loadbalancer.server.port"] == "80"
    assert params["mounts"][0]["ReadOnly"]
    assert params["mode"]["replicated"]["Replicas"] == 1


def test_removing_custom_rules_removes_proxy_and_restores_backend_routes(site_info, monkeypatch):
    site_info.type_ = "dynamic"
    client, backend, proxy = Mock(), Mock(), Mock()
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    monkeypatch.setattr(
        router.services,
        "find_service_by_name",
        lambda _client, name: proxy if name.endswith("-proxy") else backend,
    )
    assert router.update_docker_service(site_info) == {}
    assert backend.update.call_args.kwargs["labels"]
    proxy.remove.assert_called_once()
    proxy.update.assert_not_called()
    client.containers.run.assert_not_called()


def test_invalid_config_does_not_update_any_service(site_info, monkeypatch):
    site_info.custom_nginx_config = "unknown_directive on;"
    client = Mock()
    client.containers.run.side_effect = docker.errors.ContainerError(
        Mock(), 1, "nginx -t", "nginx:latest", "invalid directive"
    )
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    with pytest.raises(HTTPException) as error:
        router.update_docker_service(site_info)
    assert error.value.status_code == 400
    assert error.value.detail["user_error"] is True
    client.services.create.assert_not_called()
    client.services.list.assert_not_called()


def test_site_removal_cleans_backend_proxy_and_config(site_info, monkeypatch):
    client, backend, proxy = Mock(), Mock(), Mock()
    target = gateway.config_path(site_info)
    target.parent.mkdir(parents=True)
    target.write_text("old configuration")
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    monkeypatch.setattr(
        router.services,
        "find_service_by_name",
        lambda _client, name: proxy if name.endswith("-proxy") else backend,
    )
    assert router.remove_docker_service(site_info) == {}
    backend.remove.assert_called_once()
    proxy.remove.assert_called_once()
    assert not target.exists()


@pytest.mark.skipif(
    os.environ.get("DIRECTOR_NGINX_INTEGRATION") != "1",
    reason="Opt in to disposable Docker Nginx syntax validation.",
)
@pytest.mark.parametrize("mode", ("static", "dynamic"))
def test_real_nginx_validates_both_modes_and_preserves_config_after_invalid_edit(site_info, mode):
    site_info.type_ = mode
    site_info.custom_nginx_config = 'add_header X-Director-Test "ok" always;'
    client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    try:
        gateway.prepare_gateway(client, site_info)
        target = gateway.config_path(site_info)
        valid = target.read_text()
        site_info.custom_nginx_config = "this_directive_does_not_exist on;"
        with pytest.raises(docker.errors.ContainerError):
            gateway.prepare_gateway(client, site_info)
        assert target.read_text() == valid
        assert list(target.parent.iterdir()) == [target]
    finally:
        client.close()
