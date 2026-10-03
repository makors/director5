"""Site-confined filesystem operations using pinned, no-follow directory descriptors."""

from __future__ import annotations

# pathlib cannot express these directory-descriptor operations without reopening raced paths.
# ruff: noqa: PTH101, PTH102, PTH105, PTH106, PTH108, PTH116, PTH208
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import os
import stat
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from typing import BinaryIO

from orchestrator import settings
from orchestrator.api.docker.schema import SiteInfo

MAX_EDIT_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 10000
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class FileOperationError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalize_path(value: str) -> str:
    if len(value) > 4096 or value.startswith("/") or "\\" in value:
        raise ValueError("Use a path relative to the site directory.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Paths cannot contain control characters.")
    pieces = value.split("/")
    if ".." in pieces:
        raise ValueError("Paths cannot leave the site directory.")
    if len(pieces) > 128:
        raise ValueError("This path has too many directories.")
    if any(piece.startswith(".director-tmp-") for piece in pieces):
        raise ValueError("This filename is reserved.")
    return "/".join(piece for piece in pieces if piece not in {"", "."})


def _mkdir_open(parent: int, name: str, mode: int = 0o700) -> int:
    try:
        os.mkdir(name, mode=mode, dir_fd=parent)
    except FileExistsError:
        pass
    return os.open(name, DIRECTORY_FLAGS, dir_fd=parent)


def _check_regular(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise FileOperationError("Only regular files can be opened.", 409)
    if info.st_nlink != 1:
        raise FileOperationError("Files with multiple hard links cannot be opened.", 409)


class SiteFiles:
    def __init__(self, site: SiteInfo):
        if site.pk < 0:
            raise FileOperationError("Invalid site identifier.")
        self.site = site
        self.root_fd = -1
        self.parent_fd = -1
        self.root_name = f"{site.pk % 100:02d}"

    def __enter__(self):
        settings.SITES_DIR.mkdir(parents=True, exist_ok=True)
        base = os.open(settings.SITES_DIR, DIRECTORY_FLAGS)
        try:
            self.parent_fd = _mkdir_open(base, f"{self.site.pk // 100:02d}")
            try:
                self.root_fd = _mkdir_open(self.parent_fd, self.root_name)
            except Exception:
                os.close(self.parent_fd)
                self.parent_fd = -1
                raise
        finally:
            os.close(base)
        # Serialize Manager mutations, including optimistic editor saves, across workers.
        fcntl.flock(self.root_fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args):
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1

    @contextlib.contextmanager
    def directory(self, path: str) -> Iterator[int]:
        fd = os.dup(self.root_fd)
        try:
            for piece in normalize_path(path).split("/"):
                if not piece:
                    continue
                next_fd = os.open(piece, DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def parent(self, path: str) -> Iterator[tuple[int, str]]:
        path = normalize_path(path)
        if not path:
            raise FileOperationError("Choose a file or folder inside the site directory.")
        parent, _, name = path.rpartition("/")
        with self.directory(parent) as fd:
            yield fd, name

    def open_file(self, path: str) -> int:
        with self.parent(path) as (parent, name):
            fd = os.open(name, FILE_FLAGS, dir_fd=parent)
        try:
            _check_regular(os.fstat(fd))
        except Exception:
            os.close(fd)
            raise
        return fd

    def list_entries(self, path: str) -> list[dict]:
        path = normalize_path(path)
        entries = []
        with self.directory(path) as fd:
            names = os.listdir(fd)
            if len(names) > MAX_ENTRIES:
                raise FileOperationError("This folder has too many entries to display.", 413)
            for name in names:
                if name.startswith(".director-tmp-"):
                    continue
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = "other"
                if stat.S_ISDIR(info.st_mode):
                    kind = "directory"
                elif stat.S_ISLNK(info.st_mode):
                    kind = "link"
                elif stat.S_ISREG(info.st_mode):
                    kind = "file"
                entries.append(
                    {
                        "name": name,
                        "path": "/".join(filter(None, (path, name))),
                        "type": kind,
                        "size": info.st_size,
                        "modified": info.st_mtime,
                        "mode": f"{stat.S_IMODE(info.st_mode) & 0o777:03o}",
                    }
                )
        return sorted(
            entries, key=lambda item: (item["type"] != "directory", item["name"].casefold())
        )

    def read(self, path: str) -> dict:
        with os.fdopen(self.open_file(path), "rb") as stream:
            content = stream.read(MAX_EDIT_BYTES + 1)
            mode = f"{stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & 0o777:03o}"
        if len(content) > MAX_EDIT_BYTES:
            raise FileOperationError(
                "This file is too large for the editor. Download it instead.", 413
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileOperationError(
                "This file is not UTF-8 text. Download it instead.", 415
            ) from exc
        if "\x00" in text:
            raise FileOperationError("This file contains binary data. Download it instead.", 415)
        return {"content": text, "sha256": hashlib.sha256(content).hexdigest(), "mode": mode}

    def write(
        self,
        path: str,
        content: bytes,
        *,
        create_only: bool = False,
        expected_sha256: str | None = None,
        mode: str | None = None,
    ) -> dict:
        if len(content) > MAX_UPLOAD_BYTES:
            raise FileOperationError("Files may be at most 64 MiB.", 413)
        with self.parent(path) as (parent, name):
            permissions = 0o644
            try:
                existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if expected_sha256 is not None:
                    raise FileOperationError(
                        "The file was removed. Reload before saving.", 409
                    ) from None
            else:
                if create_only:
                    raise FileOperationError("A file or folder already uses that name.", 409)
                _check_regular(existing)
                permissions = stat.S_IMODE(existing.st_mode) & 0o777
                if expected_sha256 is not None:
                    old = self.read(path)
                    if old["sha256"] != expected_sha256:
                        raise FileOperationError("The file changed. Reload before saving.", 409)
            if mode is not None:
                permissions = int(mode, 8)
            temporary = f".director-tmp-{uuid.uuid4().hex}"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                mode=0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fchmod(stream.fileno(), permissions)
                    os.fsync(stream.fileno())
                if create_only:
                    os.link(
                        temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False
                    )
                    os.unlink(temporary, dir_fd=parent)
                else:
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)
        return {"path": normalize_path(path), "sha256": hashlib.sha256(content).hexdigest()}

    def mkdir(self, path: str) -> None:
        with self.parent(path) as (parent, name):
            os.mkdir(name, mode=0o755, dir_fd=parent)
            fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
            try:
                os.fchmod(fd, 0o755)
            finally:
                os.close(fd)

    def chmod(self, path: str, mode: str) -> None:
        with self.parent(path) as (parent, name):
            fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode):
                    _check_regular(info)
                # O_PATH lets the owner restore permissions on an unreadable file.
                os.chmod(f"/proc/self/fd/{fd}", int(mode, 8))
            finally:
                os.close(fd)

    def delete(self, path: str, *, recursive: bool = False) -> None:
        with self.parent(path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                os.unlink(name, dir_fd=parent)
            elif not recursive:
                os.rmdir(name, dir_fd=parent)
            else:
                self._remove_directory(parent, name)

    def _remove_directory(self, parent: int, name: str) -> None:
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
        try:
            for child in os.listdir(fd):
                info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    self._remove_directory(fd, child)
                else:
                    os.unlink(child, dir_fd=fd)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent)

    def move(self, source: str, destination: str) -> None:
        source, destination = normalize_path(source), normalize_path(destination)
        if destination.startswith(source + "/"):
            raise FileOperationError("A folder cannot be moved inside itself.")
        with self.parent(source) as (source_fd, source_name):
            with self.parent(destination) as (destination_fd, destination_name):
                library = ctypes.CDLL(None, use_errno=True)
                rename = getattr(library, "renameat2", None)
                if rename is None:
                    raise FileOperationError("Safe rename is not available on this server.", 501)
                rename.argtypes = [
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                ]
                rename.restype = ctypes.c_int
                result = rename(
                    source_fd,
                    os.fsencode(source_name),
                    destination_fd,
                    os.fsencode(destination_name),
                    1,
                )
                if result != 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error))

    def archive(self, path: str) -> BinaryIO:
        output = tempfile.TemporaryFile(mode="w+b")
        total = [0, 0]
        try:
            with self.directory(path) as fd:
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    self._archive_directory(fd, "", archive, total)
            output.seek(0)
            return output
        except Exception:
            output.close()
            raise

    def _archive_directory(
        self, fd: int, prefix: str, archive: zipfile.ZipFile, total: list[int]
    ) -> None:
        if prefix.count("/") > 128:
            raise FileOperationError("This folder has too many nested directories to archive.", 413)
        for name in os.listdir(fd):
            if name.startswith(".director-tmp-"):
                continue
            try:
                if normalize_path(name) != name:
                    raise ValueError("Invalid filename")
            except ValueError as exc:
                raise FileOperationError(
                    "This folder contains a filename that cannot be archived safely.", 409
                ) from exc
            total[0] += 1
            if total[0] > MAX_ENTRIES:
                raise FileOperationError("This folder has too many entries to archive.", 413)
            archive_name = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
                try:
                    archive.writestr(archive_name + "/", b"")
                    self._archive_directory(child_fd, archive_name + "/", archive, total)
                finally:
                    os.close(child_fd)
            else:
                _check_regular(info)
                file_fd = os.open(name, FILE_FLAGS, dir_fd=fd)
                with os.fdopen(file_fd, "rb") as stream:
                    _check_regular(os.fstat(stream.fileno()))
                    zip_info = zipfile.ZipInfo(archive_name)
                    zip_info.compress_type = zipfile.ZIP_DEFLATED
                    zip_info.external_attr = (stat.S_IMODE(info.st_mode) & 0o777) << 16
                    with archive.open(zip_info, "w") as target:
                        while chunk := stream.read(65536):
                            total[1] += len(chunk)
                            if total[1] > MAX_ARCHIVE_BYTES:
                                raise FileOperationError("This archive exceeds 256 MiB.", 413)
                            target.write(chunk)


def file_error(exc: OSError) -> FileOperationError:
    if exc.errno == errno.ENOENT:
        return FileOperationError("The file or folder does not exist.", 404)
    if exc.errno == errno.EEXIST:
        return FileOperationError("A file or folder already uses that name.", 409)
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return FileOperationError(
            "Symbolic links and invalid directory paths cannot be opened.", 409
        )
    if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
        return FileOperationError("The server cannot access this file or folder.", 403)
    if exc.errno == errno.ENOTEMPTY:
        return FileOperationError("This folder is not empty. Confirm recursive deletion.", 409)
    if exc.errno == errno.ENOSPC:
        return FileOperationError("The server does not have enough disk space.", 507)
    return FileOperationError("The file operation failed on the server.", 500)
