"""Database administration and user SQL endpoints."""

import psycopg
import pymysql
from fastapi import APIRouter, HTTPException

from . import services
from .schema import ManagedDatabaseRequest, QueryDatabaseRequest, RotateDatabaseRequest

router = APIRouter()
DATABASE_ERRORS = (psycopg.Error, pymysql.MySQLError, OSError)


def _administration_error() -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "description": "Database operation failed",
            "explanation": "The database server could not complete this operation. Contact an administrator if the problem continues.",
            "user_error": True,
        },
    )


@router.post("/create")
def create_database(request: ManagedDatabaseRequest):
    try:
        services.create_database(request)
    except DATABASE_ERRORS as error:
        raise _administration_error() from error
    return {"ok": True}


@router.post("/delete")
def delete_database(request: ManagedDatabaseRequest):
    try:
        services.delete_database(request)
    except DATABASE_ERRORS as error:
        raise _administration_error() from error
    return {"ok": True}


@router.post("/rotate-password")
def rotate_database_password(request: RotateDatabaseRequest):
    try:
        services.rotate_database_password(request)
    except DATABASE_ERRORS as error:
        raise _administration_error() from error
    return {"ok": True}


@router.post("/query")
def query_database(request: QueryDatabaseRequest):
    try:
        return services.query_database(request)
    except DATABASE_ERRORS as error:
        raise HTTPException(
            status_code=400,
            detail={
                "description": "Query failed",
                "explanation": str(error),
                "user_error": True,
            },
        ) from error
