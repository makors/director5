"""Verify least-privilege connection selection, SQL composition, and result bounds."""

from unittest.mock import MagicMock, patch

import psycopg
import pytest

from orchestrator.api.database import services
from orchestrator.api.database.schema import QueryDatabaseRequest, RotateDatabaseRequest

from .test_database_router import management_request


def mock_connection():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    return connection, cursor


def test_postgres_creation_grants_schema_on_site_database():
    request = management_request()
    connection, cursor = mock_connection()
    cursor.fetchone.side_effect = (None, None)
    with patch.object(services.psycopg, "connect", return_value=connection) as connect:
        services.create_database(request)
    assert connect.call_args_list[0].kwargs["dbname"] == "postgres"
    assert connect.call_args_list[1].kwargs["dbname"] == "site_123"
    assert connect.call_args_list[1].kwargs["user"] == "admin"
    commands = [
        call.args[0].as_string()
        if isinstance(call.args[0], psycopg.sql.Composable)
        else call.args[0]
        for call in cursor.execute.call_args_list
    ]
    assert any(
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION" in command for command in commands
    )
    assert any('CREATE DATABASE "site_123" OWNER "site_123"' in command for command in commands)
    assert any(
        'GRANT USAGE, CREATE ON SCHEMA public TO "site_123"' in command for command in commands
    )


def test_mysql_grants_only_site_database_to_site_user():
    request = management_request("mysql")
    connection, cursor = mock_connection()
    with patch.object(services.pymysql, "connect", return_value=connection) as connect:
        services.create_database(request)
    assert connect.call_args.kwargs["user"] == "admin"
    grants = [
        call for call in cursor.execute.call_args_list if str(call.args[0]).startswith("GRANT")
    ]
    assert grants[0].args == ("GRANT ALL PRIVILEGES ON `site_123`.* TO %s@'%%'", ("site_123",))
    assert "site-password" not in grants[0].args[0]


def test_postgres_deletion_falls_back_for_old_server():
    request = management_request()
    connection, cursor = mock_connection()
    cursor.execute.side_effect = (psycopg.errors.SyntaxError(), None, None, None)
    with patch.object(services.psycopg, "connect", return_value=connection):
        services.delete_database(request)
    assert cursor.execute.call_args_list[1].args[0].startswith("SELECT pg_terminate_backend")
    assert cursor.execute.call_args_list[-1].args[0].as_string() == 'DROP ROLE IF EXISTS "site_123"'


def test_query_uses_user_credentials_and_limits_rows_and_cells():
    request = QueryDatabaseRequest(
        database=management_request().database, sql="SELECT payload FROM entries"
    )
    connection, cursor = mock_connection()
    cursor.description = (("payload",),)
    cursor.rowcount = 101
    cursor.fetchmany.return_value = [("x" * 5000,)] * 101
    with patch.object(services.psycopg, "connect", return_value=connection) as connect:
        result = services.query_database(request)
    assert connect.call_args.kwargs["user"] == "site_123"
    assert connect.call_args.kwargs["password"] == "site-password"
    assert len(result["rows"]) == 100
    assert len(result["rows"][0][0]) == 4096
    assert result["truncated"] is True
    assert cursor.execute.call_args_list[0].args[0] == "SET LOCAL statement_timeout = '10s'"
    connection.commit.assert_called_once()


@pytest.mark.parametrize("engine", ("postgres", "mysql"))
def test_terminal_password_stays_out_of_command(engine):
    request = management_request(engine)
    command, environment = services.database_terminal_command(request.database)
    assert "site-password" not in " ".join(command)
    assert set(environment) == {"PGPASSWORD" if engine == "postgres" else "MYSQL_PWD"}


def test_rotation_uses_requested_password_instead_of_old_connection_password():
    request = management_request("mysql")
    rotate = RotateDatabaseRequest(
        database=request.database, admin=request.admin, new_password="long-new-site-password"
    )
    connection, cursor = mock_connection()
    with patch.object(services.pymysql, "connect", return_value=connection):
        services.rotate_database_password(rotate)
    assert cursor.execute.call_args.args[1] == ("site_123", "long-new-site-password")
