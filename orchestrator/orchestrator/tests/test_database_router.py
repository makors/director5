"""Verify database requests preserve failures and separate administration secrets."""

from unittest.mock import patch

import psycopg
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from orchestrator.api.database import router
from orchestrator.api.database.schema import (
    ManagedDatabaseRequest,
    QueryDatabaseRequest,
    RotateDatabaseRequest,
)


def management_request(engine="postgres"):
    port = 5432 if engine == "postgres" else 3306
    return ManagedDatabaseRequest.model_validate(
        {
            "database": {
                "url": f"{engine}://site_123:***@db.localhost:{port}/site_123",
                "name": "site_123",
                "username": "site_123",
                "password": "site-password",
            },
            "admin": {
                "hostname": "internal-db.localhost",
                "port": port,
                "username": "admin",
                "password": "admin-password",
            },
        }
    )


@pytest.mark.parametrize(
    "handler", ("create_database", "delete_database", "rotate_database_password")
)
def test_admin_failure_is_not_reported_as_success(handler):
    request = management_request()
    if handler == "rotate_database_password":
        request = RotateDatabaseRequest(
            database=request.database, admin=request.admin, new_password="long-new-site-password"
        )
    with (
        patch.object(
            router.services, handler, side_effect=psycopg.OperationalError("Connection refused")
        ),
        pytest.raises(HTTPException) as error,
    ):
        getattr(router, handler)(request)
    assert error.value.status_code == 502
    assert "admin-password" not in str(error.value.detail)
    assert error.value.detail["user_error"] is True


def test_sql_error_is_useful_to_the_site_user():
    query = QueryDatabaseRequest(
        database=management_request().database, sql="SELECT missing_column"
    )
    with (
        patch.object(
            router.services,
            "query_database",
            side_effect=psycopg.errors.UndefinedColumn("column missing_column does not exist"),
        ),
        pytest.raises(HTTPException) as error,
    ):
        router.query_database(query)
    assert error.value.status_code == 400
    assert "missing_column" in error.value.detail["explanation"]


@pytest.mark.parametrize(
    "name", ("postgres", "site_123; DROP USER admin", "site_123_other", "site_-1")
)
def test_admin_identity_cannot_target_arbitrary_database(name):
    request = management_request().model_dump()
    request["database"]["name"] = name
    request["database"]["username"] = name
    with pytest.raises(ValidationError):
        ManagedDatabaseRequest.model_validate(request)


def test_admin_password_is_redacted_in_representation():
    assert "admin-password" not in repr(management_request())
