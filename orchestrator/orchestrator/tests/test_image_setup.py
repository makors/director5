"""Approved image builds preserve files and never follow user-controlled links."""

import json
import os
import tarfile
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from orchestrator.api.docker import router
from orchestrator.api.docker.schema import ImageSetup


@pytest.fixture
def docker_client(monkeypatch):
    client = Mock()
    client.images.build.return_value = (Mock(), iter([]))
    monkeypatch.setattr(router.docker, "from_env", lambda: client)
    return client


def test_catalog_build_uses_ordered_commands_packages_and_preserves_user_dockerfile(
    site_info, docker_client
):
    site_dir = site_info.directory_path()
    user_file = site_dir / "Dockerfile"
    user_file.write_text("FROM user:custom\n")
    (site_dir / "app.py").write_text("print('hello')\n")
    site_info.image_setup = ImageSetup(
        base_image="python:3.13-alpine",
        setup_commands=["echo first", "echo second\necho third"],
        packages=["requests==2.32.3"],
        install_command_prefix="pip install",
    )
    captured = {}

    def build(**kwargs):
        assert kwargs["custom_context"] is True
        with tarfile.open(fileobj=kwargs["fileobj"]) as archive:
            captured["dockerfile"] = archive.extractfile("Dockerfile").read().decode()
            captured["app"] = archive.extractfile("app.py").read()
        return Mock(), iter([])

    docker_client.images.build.side_effect = build
    router.build_image(site_info)
    assert user_file.read_text() == "FROM user:custom\n"
    lines = captured["dockerfile"].splitlines()
    assert lines[0] == "FROM python:3.13-alpine"
    commands = [json.loads(line.removeprefix("RUN "))[2] for line in lines[1:]]
    assert commands == ["echo first", "echo second\necho third", "pip install requests==2.32.3"]
    assert captured["app"] == b"print('hello')\n"


def test_http_build_consumes_nested_image_setup_contract(client, site_info, docker_client):
    site_info.image_setup = ImageSetup(
        base_image="python:3.13-alpine", packages=["requests"], install_command_prefix="pip install"
    )

    def build(**kwargs):
        with tarfile.open(fileobj=kwargs["fileobj"]) as archive:
            dockerfile = archive.extractfile("Dockerfile").read().decode()
            assert dockerfile.startswith("FROM python:3.13-alpine\n")
            assert "pip install requests" in dockerfile
        return Mock(), iter([])

    docker_client.images.build.side_effect = build
    response = client.post(
        "/api/docker/image/build", json={"site": site_info.model_dump(mode="json")}
    )
    assert response.status_code == 200
    docker_client.images.build.assert_called_once()


def test_run_script_only_replaced_when_explicitly_requested(site_info, docker_client):
    runfile = site_info.directory_path() / "run.sh"
    runfile.write_text("keep this file")
    site_info.image_setup = ImageSetup(base_image="python:3.13-alpine")
    router.build_image(site_info)
    assert runfile.read_text() == "keep this file"
    site_info.image_setup.run_script_template = "#!/bin/sh\npython app.py\n"
    router.build_image(site_info)
    assert runfile.read_text() == site_info.image_setup.run_script_template
    assert runfile.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo"))
@pytest.mark.parametrize("target_name", ("Dockerfile", ".dockerignore", "source.txt"))
def test_build_rejects_unsafe_context_files(site_info, docker_client, tmp_path, kind, target_name):
    outside = tmp_path / "private-host-file"
    outside.write_text("PRIVATE HOST CONTENT")
    target = site_info.directory_path() / target_name
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        target.hardlink_to(outside)
    else:
        os.mkfifo(target)
    with pytest.raises(HTTPException) as error:
        router.build_image(site_info)
    assert error.value.detail["user_error"] is True
    assert outside.read_text() == "PRIVATE HOST CONTENT"
    docker_client.images.build.assert_not_called()


def test_template_write_does_not_follow_existing_link(site_info, docker_client, tmp_path):
    outside = tmp_path / "private"
    outside.write_text("keep private")
    (site_info.directory_path() / "run.sh").symlink_to(outside)
    site_info.image_setup = ImageSetup(
        base_image="python:3.13-alpine", run_script_template="overwrite"
    )
    with pytest.raises(HTTPException):
        router.build_image(site_info)
    assert outside.read_text() == "keep private"
    docker_client.images.build.assert_not_called()


def test_build_rejects_symlinked_site_directory(site_info, docker_client, tmp_path):
    site_dir = site_info.directory_path()
    outside = tmp_path / "another-site"
    outside.mkdir()
    (outside / "Dockerfile").write_text("FROM private:latest\n")
    site_dir.rmdir()
    site_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException):
        router.build_image(site_info)
    docker_client.images.build.assert_not_called()


def test_context_replacement_race_cannot_read_host_file(
    site_info, docker_client, tmp_path, monkeypatch
):
    outside = tmp_path / "private"
    outside.write_text("PRIVATE HOST CONTENT")
    source = site_info.directory_path() / "source.txt"
    source.write_text("normal site content")
    original_open = os.open
    replaced = False

    def replace_before_open(name, flags, *args, **kwargs):
        nonlocal replaced
        if name == "source.txt" and not replaced:
            replaced = True
            source.unlink()
            source.symlink_to(outside)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(HTTPException):
        router.build_image(site_info)
    assert replaced
    assert outside.read_text() == "PRIVATE HOST CONTENT"
    docker_client.images.build.assert_not_called()


def test_dockerignore_exclusion_and_negation(site_info, docker_client):
    site_dir = site_info.directory_path()
    (site_dir / ".dockerignore").write_text("*.txt\n!keep.txt\n")
    (site_dir / "ignore.txt").write_text("excluded")
    (site_dir / "keep.txt").write_text("included")

    def build(**kwargs):
        with tarfile.open(fileobj=kwargs["fileobj"]) as archive:
            assert "ignore.txt" not in archive.getnames()
            assert "keep.txt" in archive.getnames()
            assert "Dockerfile" in archive.getnames()
        return Mock(), iter([])

    docker_client.images.build.side_effect = build
    router.build_image(site_info)


@pytest.mark.parametrize(
    "setup",
    (
        {"base_image": "alpine\nRUN evil"},
        {"base_image": "alpine", "packages": ["--config"]},
        {"base_image": "alpine", "packages": ["foo;bar"], "install_command_prefix": "apk add"},
        {"base_image": "alpine", "packages": ["foo"]},
        {"base_image": "alpine", "setup_commands": ["\x00"]},
    ),
)
def test_image_setup_rejects_injection_or_missing_prefix(setup):
    with pytest.raises(ValidationError):
        ImageSetup(**setup)
