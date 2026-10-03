import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import settings
from .api.router import main_router
from .security import credential_valid

app = FastAPI(
    name="orchestrator",
    contact={"name": "Sysadmins", "email": "director@tjhsst.edu"},
)


@app.middleware("http")
async def authenticate_manager(request: Request, call_next):
    if request.url.path.startswith("/api/") and not credential_valid(
        request.headers.get("authorization")
    ):
        return JSONResponse(
            status_code=503 if not settings.APPSERVER_TOKEN else 401,
            content={"detail": "Appserver authentication is required."},
        )
    return await call_next(request)


@app.get("/ping", tags=["status"])
async def root(message: str = "pong"):
    """Check if the orchestrator is running."""
    return {"message": message}


app.include_router(main_router, prefix="/api")


@app.exception_handler(Exception)
async def handle_exception(request: Request, exc: Exception):
    """Send traceback of exceptions to the Manager."""
    return JSONResponse(
        status_code=500,
        content={
            "message": "Internal server error",
            "user_error": False,
            "exception": traceback.format_exception(exc),
        },
    )
