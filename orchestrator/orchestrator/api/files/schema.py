from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from orchestrator.api.docker.schema import SiteInfo

from .storage import MAX_EDIT_BYTES, MAX_UPLOAD_BYTES, normalize_path


class FileRequest(BaseModel):
    site: SiteInfo
    path: Annotated[str, Field(max_length=4096)] = ""

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return normalize_path(value)


class WriteRequest(FileRequest):
    content: Annotated[str, Field(max_length=MAX_EDIT_BYTES)] = ""
    create_only: bool = False
    expected_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    mode: Annotated[str, Field(pattern=r"^[0-7]{3}$")] | None = None


class UploadRequest(FileRequest):
    content_base64: Annotated[str, Field(max_length=4 * ((MAX_UPLOAD_BYTES + 2) // 3))]
    create_only: bool = True


class DeleteRequest(FileRequest):
    recursive: bool = False


class MoveRequest(FileRequest):
    destination: Annotated[str, Field(max_length=4096)]

    @field_validator("destination")
    @classmethod
    def safe_destination(cls, value: str) -> str:
        return normalize_path(value)


class ChmodRequest(FileRequest):
    mode: Annotated[str, Field(pattern=r"^[0-7]{3}$")]
