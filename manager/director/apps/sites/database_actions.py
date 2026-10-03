"""Database-only appserver payloads and operation callbacks."""

from collections.abc import Iterator

from .actions import raise_by_recoverability
from .appserver import Appserver
from .models import Database, Site
from .operations import UserFacingError


def database_management_payload(site: Site) -> dict:
    database = site.database
    if database is None:
        raise UserFacingError("This site does not have a database.")
    host = database.host
    return {
        "database": database.serialize_for_appserver(),
        "admin": {
            "hostname": host.admin_hostname or host.hostname,
            "port": host.admin_port or host.port,
            "username": host.admin_username,
            "password": host.admin_password,
        },
    }


def _request_database(
    site: Site, appservers: list[Appserver], endpoint: str, payload: dict
) -> dict:
    response = appservers[0].http_request(f"/api/database/{endpoint}", method="POST", data=payload)
    raise_by_recoverability(site, response)
    result = response.json()
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise UserFacingError(
            "The database server did not confirm that this operation completed. Please retry."
        )
    return result


def create_site_database(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    yield "Creating database and its user account."
    _request_database(site, appservers, "create", database_management_payload(site))
    site.database.provisioned = True
    site.database.save(update_fields=["provisioned"])
    yield "Database created."


def delete_site_database(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    if site.database is None:
        yield "No database to remove."
        return
    yield "Removing database and its user account."
    _request_database(site, appservers, "delete", database_management_payload(site))
    yield "Database removed."


def forget_site_database(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    database = site.database
    if database is not None:
        database.delete()
        site.refresh_from_db()
    yield "Database connection removed from this site."


def rotate_database_password(site: Site, appservers: list[Appserver]) -> Iterator[str]:
    database = site.database
    if database is None:
        raise UserFacingError("This site does not have a database.")
    password = database.pending_password or database.password
    payload = database_management_payload(site)
    payload["new_password"] = password
    yield "Changing the database password."
    _request_database(site, appservers, "rotate-password", payload)
    database.password = password
    database.pending_password = ""
    database.save(update_fields=["password", "pending_password"])
    yield "Database password changed."


def database_query_payload(database: Database, sql: str) -> dict:
    return {
        "database": database.serialize_for_appserver(),
        "connection": {
            "hostname": database.host.admin_hostname or database.host.hostname,
            "port": database.host.admin_port or database.host.port,
        },
        "sql": sql,
    }


def database_runtime_options(database: Database) -> dict:
    host = database.host
    common = ["--host", host.hostname, "--port", str(host.port)]
    if host.dbms == "postgres":
        command = [
            "psql",
            *common,
            "--username",
            database.username,
            "--dbname",
            database.username,
            "--no-psqlrc",
        ]
        environment = {"PGPASSWORD": database.password}
    else:
        command = ["mysql", *common, "--user", database.username, "--database", database.username]
        environment = {"MYSQL_PWD": database.password}
    return {"kind": "database", "command": command, "environment": environment}
