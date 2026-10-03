from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator import settings
from orchestrator.api.docker.schema import SiteInfo
from orchestrator.main import app


@pytest.fixture(autouse=True)
def isolate_site_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SITES_DIR", tmp_path)
    monkeypatch.setattr(settings, "HOST_SITES_DIR", tmp_path)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def site_info() -> SiteInfo:
    return SiteInfo(
        pk=0,
        hosts=["example.localhost.com"],
        is_served=True,
        type_="static",
        resource_limits={"cpus": 1, "memory": "1MiB", "max_request_body_size": 2 * 1024 * 1024},
        docker={"base": "python:3.12-alpine"},
    )


@pytest.fixture
def db_site_info() -> SiteInfo:
    return SiteInfo(
        pk=0,
        hosts=["example.localhost.com"],
        is_served=True,
        type_="static",
        resource_limits={"cpus": 1, "memory": "1MiB", "max_request_body_size": 2 * 1024 * 1024},
        docker={"base": "python:3.12-alpine"},
        db={
            "url": "mysql://user:password@localhost:3306/db",
            "name": "db",
            "username": "user",
            "password": "password",
        },
    )
