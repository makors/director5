import base64
import binascii
import contextlib
import os
import shutil
from collections.abc import Iterator
from typing import BinaryIO
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..docker.schema import SiteInfo
from .schema import (
    ChmodRequest,
    DeleteRequest,
    FileRequest,
    MoveRequest,
    UploadRequest,
    WriteRequest,
)
from .storage import MAX_EDIT_BYTES, FileOperationError, SiteFiles, file_error

router = APIRouter()


@contextlib.contextmanager
def site_files(site: SiteInfo) -> Iterator[SiteFiles]:
    try:
        with SiteFiles(site) as storage:
            yield storage
    except OSError as exc:
        error = file_error(exc)
        raise HTTPException(status_code=error.status, detail=str(error)) from exc
    except FileOperationError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.post("/list")
def list_files(request: FileRequest):
    with site_files(request.site) as storage:
        return {"path": request.path, "entries": storage.list_entries(request.path)}


@router.post("/read")
def read_file(request: FileRequest):
    with site_files(request.site) as storage:
        return storage.read(request.path) | {"path": request.path}


@router.post("/write")
def write_file(request: WriteRequest):
    content = request.content.encode("utf-8")
    if len(content) > MAX_EDIT_BYTES:
        raise HTTPException(status_code=413, detail="This file is too large for the editor.")
    with site_files(request.site) as storage:
        return storage.write(
            request.path,
            content,
            create_only=request.create_only,
            expected_sha256=request.expected_sha256,
            mode=request.mode,
        )


@router.post("/upload")
def upload_file(request: UploadRequest):
    try:
        content = base64.b64decode(request.content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(
            status_code=400, detail="The uploaded file could not be decoded."
        ) from exc
    with site_files(request.site) as storage:
        return storage.write(request.path, content, create_only=request.create_only)


@router.post("/mkdir")
def make_directory(request: FileRequest):
    with site_files(request.site) as storage:
        storage.mkdir(request.path)
    return {"path": request.path}


@router.post("/chmod")
def change_permissions(request: ChmodRequest):
    with site_files(request.site) as storage:
        storage.chmod(request.path, request.mode)
    return {"path": request.path, "mode": request.mode}


@router.post("/delete")
def delete_file(request: DeleteRequest):
    with site_files(request.site) as storage:
        storage.delete(request.path, recursive=request.recursive)
    return {"path": request.path}


@router.post("/move")
def move_file(request: MoveRequest):
    with site_files(request.site) as storage:
        storage.move(request.path, request.destination)
    return {"path": request.destination}


def stream_file(stream: BinaryIO) -> Iterator[bytes]:
    with stream:
        while chunk := stream.read(65536):
            yield chunk


class AttachmentResponse(StreamingResponse):
    def __init__(self, stream: BinaryIO, **kwargs):
        self.file_stream = stream
        super().__init__(stream_file(stream), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.file_stream.close()


def attachment(stream: BinaryIO, filename: str, media_type: str) -> StreamingResponse:
    return AttachmentResponse(
        stream,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/download")
def download_file(request: FileRequest):
    with site_files(request.site) as storage:
        stream = os.fdopen(storage.open_file(request.path), "rb")
    return attachment(stream, request.path.rsplit("/", 1)[-1], "application/octet-stream")


@router.post("/archive")
def download_archive(request: FileRequest):
    with site_files(request.site) as storage:
        stream = storage.archive(request.path)
    name = request.path.rsplit("/", 1)[-1] or f"site-{request.site.pk}"
    return attachment(stream, name + ".zip", "application/zip")


@router.post("/delete-all")
def delete_all_site_files(site: SiteInfo):
    with site_files(site) as storage:
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(storage.root_name, dir_fd=storage.parent_fd)
    return {}
