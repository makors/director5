import asyncio
import base64
import io
import os
import stat
import zipfile

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from orchestrator.api.docker.schema import SiteInfo
from orchestrator.api.files import router, storage
from orchestrator.api.files.schema import (
    ChmodRequest,
    DeleteRequest,
    FileRequest,
    MoveRequest,
    UploadRequest,
    WriteRequest,
)


def test_text_editor_create_read_save_and_conflict(site_info: SiteInfo):
    router.make_directory(FileRequest(site=site_info, path="public"))
    created = router.write_file(
        WriteRequest(site=site_info, path="public/index.html", content="Hello", create_only=True)
    )
    read = router.read_file(FileRequest(site=site_info, path="public/index.html"))
    assert read["content"] == "Hello"
    assert read["sha256"] == created["sha256"]
    assert read["mode"] == "644"
    router.write_file(
        WriteRequest(
            site=site_info,
            path="public/index.html",
            content="Updated",
            expected_sha256=read["sha256"],
        )
    )
    with pytest.raises(HTTPException) as error:
        router.write_file(
            WriteRequest(
                site=site_info,
                path="public/index.html",
                content="Stale",
                expected_sha256=read["sha256"],
            )
        )
    assert error.value.status_code == 409
    assert (
        router.read_file(FileRequest(site=site_info, path="public/index.html"))["content"]
        == "Updated"
    )


def test_binary_upload_and_download_bytes(site_info: SiteInfo):
    content = b"\x00\xff\x01binary payload"
    router.upload_file(
        UploadRequest(
            site=site_info, path="image.bin", content_base64=base64.b64encode(content).decode()
        )
    )
    with storage.SiteFiles(site_info) as files:
        with os.fdopen(files.open_file("image.bin"), "rb") as downloaded:
            assert downloaded.read() == content
    with pytest.raises(HTTPException) as error:
        router.read_file(FileRequest(site=site_info, path="image.bin"))
    assert error.value.status_code == 415


def test_upload_does_not_overwrite_without_confirmation(site_info: SiteInfo):
    router.write_file(WriteRequest(site=site_info, path="existing.txt", content="Original"))
    with pytest.raises(HTTPException) as error:
        router.upload_file(
            UploadRequest(site=site_info, path="existing.txt", content_base64="TmV3")
        )
    assert error.value.status_code == 409
    assert (
        router.read_file(FileRequest(site=site_info, path="existing.txt"))["content"] == "Original"
    )


def test_file_browser_lists_and_sorts_metadata(site_info: SiteInfo):
    router.make_directory(FileRequest(site=site_info, path="public"))
    router.write_file(
        WriteRequest(site=site_info, path="run.sh", content="#!/bin/sh\n", mode="755")
    )
    listing = router.list_files(FileRequest(site=site_info))
    assert [entry["name"] for entry in listing["entries"]] == ["public", "run.sh"]
    assert listing["entries"][1]["mode"] == "755"
    assert listing["entries"][1]["size"] == 10


def test_move_never_clobbers_existing_destination(site_info: SiteInfo):
    router.write_file(WriteRequest(site=site_info, path="one.txt", content="One"))
    router.write_file(WriteRequest(site=site_info, path="two.txt", content="Two"))
    with pytest.raises(HTTPException) as error:
        router.move_file(MoveRequest(site=site_info, path="one.txt", destination="two.txt"))
    assert error.value.status_code == 409
    router.move_file(MoveRequest(site=site_info, path="one.txt", destination="renamed.txt"))
    assert router.read_file(FileRequest(site=site_info, path="renamed.txt"))["content"] == "One"
    assert router.read_file(FileRequest(site=site_info, path="two.txt"))["content"] == "Two"


def test_chmod_allows_execute_without_special_bits(site_info: SiteInfo):
    router.write_file(WriteRequest(site=site_info, path="run.sh", content="#!/bin/sh\n"))
    router.change_permissions(ChmodRequest(site=site_info, path="run.sh", mode="755"))
    assert stat.S_IMODE((site_info.directory_path() / "run.sh").stat().st_mode) == 0o755
    with pytest.raises(ValidationError):
        ChmodRequest(site=site_info, path="run.sh", mode="4755")


def test_owner_can_restore_unreadable_file_permissions(site_info: SiteInfo):
    router.write_file(WriteRequest(site=site_info, path="index.txt", content="Hello"))
    router.change_permissions(ChmodRequest(site=site_info, path="index.txt", mode="000"))
    router.change_permissions(ChmodRequest(site=site_info, path="index.txt", mode="644"))
    assert router.read_file(FileRequest(site=site_info, path="index.txt"))["content"] == "Hello"


@pytest.mark.parametrize(
    "path", ("../secret", "/etc/passwd", "public/../../secret", "public\\..\\secret", "bad\x00name")
)
def test_unsafe_paths_rejected(site_info: SiteInfo, path: str):
    with pytest.raises(ValidationError):
        FileRequest(site=site_info, path=path)


def test_symlink_reads_writes_and_archive_cannot_escape(site_info: SiteInfo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("Secret")
    root = site_info.directory_path()
    (root / "link").symlink_to(outside, target_is_directory=True)
    for call in (
        lambda: router.read_file(FileRequest(site=site_info, path="link/secret.txt")),
        lambda: router.write_file(
            WriteRequest(site=site_info, path="link/secret.txt", content="Changed")
        ),
        lambda: router.download_archive(FileRequest(site=site_info)),
    ):
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 409
    assert secret.read_text() == "Secret"


def test_recursive_delete_unlinks_symlink_without_touching_target(site_info: SiteInfo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("Secret")
    root = site_info.directory_path()
    (root / "folder").mkdir()
    (root / "folder" / "link").symlink_to(outside, target_is_directory=True)
    router.delete_file(DeleteRequest(site=site_info, path="folder", recursive=True))
    assert not (root / "folder").exists()
    assert secret.read_text() == "Secret"


def test_racing_directory_symlink_swap_keeps_write_in_pinned_directory(
    site_info: SiteInfo, tmp_path, monkeypatch
):
    root = site_info.directory_path()
    public = root / "public"
    public.mkdir()
    (public / "index.txt").write_text("Original")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.txt").write_text("Secret")
    original_replace = storage.os.replace

    def swap_then_replace(source, destination, **kwargs):
        public.rename(root / "pinned")
        public.symlink_to(outside, target_is_directory=True)
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr(storage.os, "replace", swap_then_replace)
    router.write_file(WriteRequest(site=site_info, path="public/index.txt", content="Updated"))
    assert (root / "pinned" / "index.txt").read_text() == "Updated"
    assert (outside / "index.txt").read_text() == "Secret"


def test_hard_link_cannot_expose_or_modify_outside_file(site_info: SiteInfo, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("Secret")
    os.link(secret, site_info.directory_path() / "hardlink.txt")
    with pytest.raises(HTTPException) as error:
        router.read_file(FileRequest(site=site_info, path="hardlink.txt"))
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        router.write_file(WriteRequest(site=site_info, path="hardlink.txt", content="Changed"))
    assert secret.read_text() == "Secret"


def test_archive_preserves_files_and_permissions(site_info: SiteInfo):
    router.make_directory(FileRequest(site=site_info, path="public"))
    router.write_file(WriteRequest(site=site_info, path="public/index.html", content="Hello"))
    router.write_file(
        WriteRequest(site=site_info, path="run.sh", content="#!/bin/sh\n", mode="755")
    )
    with storage.SiteFiles(site_info) as files:
        with files.archive("") as archive:
            data = archive.read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.read("public/index.html") == b"Hello"
        assert archive.getinfo("run.sh").external_attr >> 16 == 0o755


def test_archive_rejects_unsafe_windows_filename(site_info: SiteInfo):
    (site_info.directory_path() / "..\\escape.txt").write_text("Unsafe archive path")
    with pytest.raises(HTTPException) as error:
        router.download_archive(FileRequest(site=site_info))
    assert error.value.status_code == 409


def test_download_closes_file_when_client_disconnects_before_body():
    stream = io.BytesIO(b"Download content")
    response = router.attachment(stream, "file.txt", "application/octet-stream")

    async def receive():
        return {"type": "http.disconnect"}

    async def broken_send(_message):
        raise BrokenPipeError("Client disconnected")

    async def request():
        with pytest.raises(ExceptionGroup) as error:
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, broken_send)
        assert isinstance(error.value.exceptions[0], BrokenPipeError)

    asyncio.run(request())
    assert stream.closed


@pytest.mark.parametrize("operation", ("delete", "move", "write", "chmod"))
def test_site_root_cannot_be_mutated(site_info: SiteInfo, operation: str):
    calls = {
        "delete": lambda: router.delete_file(DeleteRequest(site=site_info, recursive=True)),
        "move": lambda: router.move_file(MoveRequest(site=site_info, destination="other")),
        "write": lambda: router.write_file(WriteRequest(site=site_info, content="Bad")),
        "chmod": lambda: router.change_permissions(ChmodRequest(site=site_info, mode="000")),
    }
    with pytest.raises(HTTPException) as error:
        calls[operation]()
    assert error.value.status_code == 400
