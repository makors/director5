"""Separate database administration secrets from ordinary site configuration."""

from typing import Annotated

from pydantic import BaseModel, Field, SecretStr, model_validator

from ..docker.schema import DatabaseInfo


class ConnectionAddress(BaseModel):
    hostname: Annotated[str, Field(min_length=1, max_length=255)]
    port: Annotated[int, Field(ge=1, le=65535)]


class AdminConnection(ConnectionAddress):
    username: Annotated[str, Field(min_length=1, max_length=255)]
    password: SecretStr


class ManagedDatabaseRequest(BaseModel):
    database: DatabaseInfo
    admin: AdminConnection

    @model_validator(mode="after")
    def validate_identity(self):
        name = self.database.name
        if not name.startswith("site_") or not name[5:].isdigit() or self.database.username != name:
            raise ValueError("Managed database names and users must match site_<numeric ID>.")
        return self


class RotateDatabaseRequest(ManagedDatabaseRequest):
    new_password: Annotated[SecretStr, Field(min_length=20, max_length=255)]


class QueryDatabaseRequest(BaseModel):
    database: DatabaseInfo
    connection: ConnectionAddress | None = None
    sql: Annotated[str, Field(min_length=1, max_length=100_000)]
