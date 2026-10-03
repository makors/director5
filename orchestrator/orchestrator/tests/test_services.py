import os
import stat
from urllib.parse import unquote, urlsplit

import pytest
from pydantic import ValidationError

from orchestrator import settings
from orchestrator.api.docker import services
from orchestrator.api.docker.schema import DatabaseInfo, SiteInfo


def test_site_info_env(site_info: SiteInfo):
    env = site_info.container_env()
    assert env["TZ"] == settings.TIMEZONE
    assert "DATABASE_URL" not in env
    assert "DIRECTOR_DATABASE_URL" not in env
    assert "DIRECTOR_DATABASE_TYPE" not in env
    assert "DIRECTOR_DATABASE_HOST" not in env
    assert "DIRECTOR_DATABASE_PORT" not in env
    assert "DIRECTOR_DATABASE_NAME" not in env
    assert "DIRECTOR_DATABASE_USERNAME" not in env
    assert "DIRECTOR_DATABASE_PASSWORD" not in env


def test_site_info_db_env(db_site_info: SiteInfo):
    env = db_site_info.container_env()
    assert env["TZ"] == settings.TIMEZONE
    assert db_site_info.db is not None
    assert env["DATABASE_URL"] == "mysql://user:password@localhost:3306/db"
    assert env["DIRECTOR_DATABASE_URL"] == env["DATABASE_URL"]
    assert env["DIRECTOR_DATABASE_TYPE"] == db_site_info.db.type_
    assert env["DIRECTOR_DATABASE_HOST"] == db_site_info.db.host
    assert env["DIRECTOR_DATABASE_PORT"] == db_site_info.db.port
    assert env["DIRECTOR_DATABASE_NAME"] == db_site_info.db.name
    assert env["DIRECTOR_DATABASE_USERNAME"] == db_site_info.db.username
    assert env["DIRECTOR_DATABASE_PASSWORD"] == db_site_info.db.password


def test_shared_params(site_info: SiteInfo):
    site_info.type_ = "dynamic"
    params = services.shared_swarm_params(site_info)
    assert site_info.directory_path().exists()
    assert any({"Target": "/site"}.items() <= mount.items() for mount in params["mounts"]), (
        "Expected a /site mount"
    )


def test_new_public_directory_is_readable_with_restrictive_umask(site_info: SiteInfo):
    previous_umask = os.umask(0o077)
    try:
        services.shared_swarm_params(site_info)
    finally:
        os.umask(previous_umask)
    public_dir = site_info.directory_path() / "public"
    assert stat.S_IMODE(public_dir.stat().st_mode) == 0o755


def test_existing_public_permissions_are_preserved(site_info: SiteInfo):
    public_dir = site_info.directory_path() / "public"
    public_dir.mkdir()
    public_dir.chmod(0o700)
    index = public_dir / "index.html"
    index.write_text("Content")
    index.chmod(0o600)

    services.shared_swarm_params(site_info)

    assert stat.S_IMODE(public_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(index.stat().st_mode) == 0o600


def test_update_service_params(site_info: SiteInfo):
    site_info.runfile = "runfile.sh"
    site_info.type_ = "dynamic"
    params = services.create_service_params(site_info)
    assert params["name"] == f"site_{site_info.pk:04d}"
    assert any("runfile.sh" in cmd for cmd in params["command"])
    assert params["workdir"] == "/site/public"
    assert "director-sites" in params["networks"]
    traefik_labels = {
        f"traefik.http.routers.{site_info}.rule": f"Host(`{site_info.hosts[0]}`)",
        f"traefik.http.services.{site_info}.loadbalancer.server.port": "80",
        "traefik.swarm.network": "director-sites",
    }
    assert traefik_labels.items() <= params["labels"].items()


@pytest.mark.parametrize("db_type", ("postgres", "mysql"))
def test_database_url_uses_site_credentials(db_type: str):
    db = DatabaseInfo.model_validate(
        {
            "url": f"{db_type}://site_1:***@localhost:5432/ignored",
            "name": "site-data",
            "username": "site_1",
            "password": "p@ss:#/ word",
        }
    )
    connection = urlsplit(db.connection_url)
    assert connection.scheme == db_type
    assert connection.hostname == "localhost"
    assert connection.username == "site_1"
    assert connection.password is not None
    assert unquote(connection.password) == db.password
    assert connection.path == "/site-data"
    assert connection.fragment == ""


def test_static_service_uses_mounted_workdir(site_info: SiteInfo):
    params = services.create_service_params(site_info)
    assert params["workdir"] == "/usr/share/nginx/html"
    assert any(mount["Target"] == params["workdir"] for mount in params["mounts"])
    assert "command" not in params


def test_service_restart_and_shutdown_delays(site_info: SiteInfo):
    params = services.create_service_params(site_info)
    assert params["stop_grace_period"] == 3_000_000_000
    assert params["restart_policy"]["Delay"] == 5_000_000_000


def test_shared_host_path_routing(site_info: SiteInfo):
    site = SiteInfo.model_validate(
        site_info.model_dump()
        | {
            "hosts": ["custom.example.com", "user.localhost"],
            "routes": [
                {"host": "custom.example.com"},
                {"host": "user.localhost", "path_prefix": "/my-site"},
            ],
        }
    )
    labels = services.create_service_params(site)["labels"]
    assert labels[f"traefik.http.routers.{site}-0.rule"] == "Host(`custom.example.com`)"
    assert labels[f"traefik.http.routers.{site}-1.rule"] == (
        "Host(`user.localhost`) && (Path(`/my-site`) || PathPrefix(`/my-site/`))"
    )
    assert labels[f"traefik.http.middlewares.{site}-1-strip-prefix.stripprefix.prefixes"] == (
        "/my-site"
    )
    assert labels[f"traefik.http.routers.{site}-1.middlewares"].endswith(
        f",{site}-1-strip-prefix@swarm"
    )
    assert "strip-prefix" not in labels[f"traefik.http.routers.{site}-0.middlewares"]


@pytest.mark.parametrize("path_prefix", ("/site`)", "/site?x=1", "/site#fragment", "//site/"))
def test_route_rejects_invalid_path(site_info: SiteInfo, path_prefix: str):
    with pytest.raises(ValidationError):
        SiteInfo.model_validate(
            site_info.model_dump()
            | {"routes": [{"host": "user.localhost", "path_prefix": path_prefix}]}
        )


@pytest.mark.parametrize("runfile", ("run.sh; echo unsafe", "$(echo unsafe)", "run.sh\n"))
def test_runfile_requires_a_single_safe_path(site_info: SiteInfo, runfile: str):
    with pytest.raises(ValidationError):
        SiteInfo.model_validate(site_info.model_dump() | {"runfile": runfile})


def test_default_runfile_does_not_mutate_site(site_info: SiteInfo):
    first = services.create_service_params(site_info)
    second = services.create_service_params(site_info)
    assert site_info.runfile is None
    assert first == second
