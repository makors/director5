"""Build Docker archives without following user-controlled host filesystem paths."""

import io
import os
import stat
import tarfile
import tempfile
from typing import BinaryIO

from docker.utils.build import PatternMatcher

from orchestrator.api.files.storage import (
    DIRECTORY_FLAGS,
    FILE_FLAGS,
    MAX_ARCHIVE_BYTES,
    MAX_ENTRIES,
    FileOperationError,
    SiteFiles,
    normalize_path,
)


def build_context(files: SiteFiles, dockerfile: str) -> BinaryIO:
    """Return a bounded tar archive with a trusted Dockerfile entry."""
    try:
        ignore = files.read(".dockerignore")["content"].splitlines()
    except FileNotFoundError:
        ignore = []
    patterns = PatternMatcher(
        [line.strip() for line in ignore if line.strip() and not line.startswith("#")]
    )
    output = tempfile.TemporaryFile(mode="w+b")
    limits = [0, 0]
    try:
        with tarfile.open(mode="w", fileobj=output) as archive:
            _add_directory(files.root_fd, "", archive, patterns, limits)
            content = dockerfile.encode("utf-8")
            header = tarfile.TarInfo("Dockerfile")
            header.size = len(content)
            header.mode = 0o644
            archive.addfile(header, io.BytesIO(content))
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise


def _add_directory(
    fd: int, prefix: str, archive: tarfile.TarFile, patterns: PatternMatcher, limits: list[int]
) -> None:
    if prefix.count("/") > 128:
        raise FileOperationError("The build context has too many nested folders.", 413)
    for name in os.listdir(fd):  # noqa: PTH208 -- keep traversal pinned to its directory descriptor
        if name.startswith(".director-tmp-") or (not prefix and name == "Dockerfile"):
            continue
        _validate_member_name(name)
        limits[0] += 1
        if limits[0] > MAX_ENTRIES:
            raise FileOperationError("The build context contains too many files.", 413)
        relative = prefix + name
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)  # noqa: PTH116
        except FileNotFoundError:
            continue
        ignored = patterns.matches(relative)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
            try:
                if not ignored:
                    header = tarfile.TarInfo(relative + "/")
                    header.type = tarfile.DIRTYPE
                    header.mode = stat.S_IMODE(info.st_mode) & 0o777
                    archive.addfile(header)
                _add_directory(child, relative + "/", archive, patterns, limits)
            finally:
                os.close(child)
        elif not ignored:
            _add_file(fd, name, relative, archive, limits)


def _validate_member_name(name: str) -> None:
    try:
        if normalize_path(name) != name:
            raise ValueError("Invalid filename")
    except ValueError as exc:
        raise FileOperationError(
            "The build context contains an unsupported filename.", 409
        ) from exc


def _add_file(
    fd: int, name: str, relative: str, archive: tarfile.TarFile, limits: list[int]
) -> None:
    file_fd = os.open(name, FILE_FLAGS, dir_fd=fd)
    with os.fdopen(file_fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise FileOperationError(
                "Build files must be regular files without symbolic or multiple hard links.", 409
            )
        limits[1] += info.st_size
        if limits[1] > MAX_ARCHIVE_BYTES:
            raise FileOperationError("The build context exceeds 256 MiB.", 413)
        header = tarfile.TarInfo(relative)
        header.size = info.st_size
        header.mode = stat.S_IMODE(info.st_mode) & 0o777
        archive.addfile(header, source)
