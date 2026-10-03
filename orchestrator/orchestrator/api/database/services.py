"""Real PostgreSQL and MySQL provisioning and site-user SQL execution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pymysql
from psycopg import sql

from ..docker.schema import DatabaseInfo
from .schema import (
    ConnectionAddress,
    ManagedDatabaseRequest,
    QueryDatabaseRequest,
    RotateDatabaseRequest,
)

QUERY_ROW_LIMIT = 100
QUERY_CELL_LIMIT = 4096


@contextmanager
def connect_database(
    database: DatabaseInfo,
    *,
    address: ConnectionAddress | None = None,
    username: str | None = None,
    password: str | None = None,
    admin: bool = False,
) -> Iterator[Any]:
    hostname = address.hostname if address else database.host
    port = address.port if address else database.port
    connection_username = username if username is not None else database.username
    connection_password = password if password is not None else database.password
    connection: psycopg.Connection | pymysql.connections.Connection
    if database.type_ == "postgres":
        connection = psycopg.connect(
            user=connection_username,
            password=connection_password,
            port=port,
            host=hostname,
            dbname="postgres" if admin else database.name,
            connect_timeout=5,
            autocommit=admin,
        )
    else:
        unix_socket = None
        if hostname.startswith("/"):
            unix_socket = hostname
            hostname = "localhost"
        connection = pymysql.connect(
            user=connection_username,
            password=connection_password,
            port=port,
            unix_socket=unix_socket,
            host=hostname,
            database="mysql" if admin else database.name,
            connect_timeout=5,
            read_timeout=15,
            write_timeout=15,
            charset="utf8mb4",
            autocommit=admin,
        )
    try:
        yield connection
    finally:
        connection.close()


def _admin_connection(request: ManagedDatabaseRequest):
    return connect_database(
        request.database,
        address=request.admin,
        username=request.admin.username,
        password=request.admin.password.get_secret_value(),
        admin=True,
    )


def _mysql_identifier(name: str) -> str:
    # The management schema guarantees generated names, so quoting never changes identity.
    return "`" + name.replace("`", "``") + "`"


def create_database(request: ManagedDatabaseRequest) -> None:
    database = request.database
    with _admin_connection(request) as connection, connection.cursor() as cursor:
        if database.type_ == "postgres":
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (database.username,))
            exists = cursor.fetchone() is not None
            verb = "ALTER ROLE" if exists else "CREATE ROLE"
            cursor.execute(
                sql.SQL(
                    verb + " {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}"
                ).format(
                    sql.Identifier(database.username),
                    sql.Literal(database.password),
                )
            )
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database.name,))
            if cursor.fetchone() is None:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(database.name),
                        sql.Identifier(database.username),
                    )
                )
            cursor.execute(
                sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO {}").format(
                    sql.Identifier(database.name),
                    sql.Identifier(database.username),
                )
            )
        else:
            cursor.execute(
                "CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s",
                (database.username, database.password),
            )
            cursor.execute(
                "ALTER USER %s@'%%' IDENTIFIED BY %s", (database.username, database.password)
            )
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {_mysql_identifier(database.name)} CHARACTER SET utf8mb4"
            )
            cursor.execute(
                f"GRANT ALL PRIVILEGES ON {_mysql_identifier(database.name)}.* TO %s@'%%'",
                (database.username,),
            )
    if database.type_ == "postgres":
        # Grant on the site's database, rather than accidentally on the admin database.
        with (
            connect_database(
                database,
                address=request.admin,
                username=request.admin.username,
                password=request.admin.password.get_secret_value(),
                admin=False,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(
                    sql.Identifier(database.username)
                )
            )
            connection.commit()


def delete_database(request: ManagedDatabaseRequest) -> None:
    database = request.database
    with _admin_connection(request) as connection, connection.cursor() as cursor:
        if database.type_ == "postgres":
            # PostgreSQL 13+ can remove an in-use database atomically; older versions use termination.
            try:
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database.name)
                    )
                )
            except (psycopg.errors.SyntaxError, psycopg.errors.FeatureNotSupported):
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database.name,),
                )
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database.name))
                )
            cursor.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(database.username))
            )
        else:
            cursor.execute(f"DROP DATABASE IF EXISTS {_mysql_identifier(database.name)}")
            cursor.execute("DROP USER IF EXISTS %s@'%%'", (database.username,))


def rotate_database_password(request: RotateDatabaseRequest) -> None:
    with _admin_connection(request) as connection, connection.cursor() as cursor:
        password = request.new_password.get_secret_value()
        if request.database.type_ == "postgres":
            cursor.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(request.database.username), sql.Literal(password)
                )
            )
        else:
            cursor.execute(
                "ALTER USER %s@'%%' IDENTIFIED BY %s", (request.database.username, password)
            )


def _query_text(value: Any) -> str:
    return value.hex() if isinstance(value, bytes) else str(value)


def _query_value(value: Any) -> str | None:
    if value is None:
        return None
    value = _query_text(value)
    return value if len(value) <= QUERY_CELL_LIMIT else value[: QUERY_CELL_LIMIT - 1] + "…"


def query_database(request: QueryDatabaseRequest) -> dict:
    with (
        connect_database(request.database, address=request.connection) as connection,
        connection.cursor() as cursor,
    ):
        if request.database.type_ == "postgres":
            cursor.execute("SET LOCAL statement_timeout = '10s'")
        else:
            # MySQL bounds SELECT execution; the driver also bounds socket reads and writes.
            try:
                cursor.execute("SET SESSION MAX_EXECUTION_TIME = 10000")
            except pymysql.err.OperationalError as error:
                if error.args[0] != 1193:  # MariaDB uses max_statement_time instead.
                    raise
                cursor.execute("SET SESSION max_statement_time = 10")
        cursor.execute(request.sql)
        columns = [column[0] for column in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(QUERY_ROW_LIMIT + 1) if columns else []
        result = {
            "columns": columns,
            "rows": [[_query_value(value) for value in row] for row in rows[:QUERY_ROW_LIMIT]],
            "affected_rows": max(cursor.rowcount, 0),
            "truncated": len(rows) > QUERY_ROW_LIMIT,
            "cells_truncated": any(
                value is not None and len(_query_text(value)) > QUERY_CELL_LIMIT
                for row in rows[:QUERY_ROW_LIMIT]
                for value in row
            ),
        }
        connection.commit()
        return result


def database_terminal_command(
    database: DatabaseInfo, address: ConnectionAddress | None = None
) -> tuple[list[str], dict[str, str]]:
    hostname = address.hostname if address else database.host
    port = address.port if address else database.port
    if database.type_ == "postgres":
        return (
            [
                "psql",
                "--host",
                hostname,
                "--port",
                str(port),
                "--username",
                database.username,
                "--dbname",
                database.name,
                "--no-psqlrc",
            ],
            {"PGPASSWORD": database.password},
        )
    return (
        [
            "mysql",
            "--host",
            hostname,
            "--port",
            str(port),
            "--user",
            database.username,
            "--database",
            database.name,
        ],
        {"MYSQL_PWD": database.password},
    )
