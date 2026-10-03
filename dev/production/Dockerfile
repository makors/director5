# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.6-python3.13-bookworm-slim

ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/venv PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 TZ=UTC
WORKDIR /director5
COPY pyproject.toml uv.lock README.md ./
COPY manager/pyproject.toml manager/pyproject.toml
COPY orchestrator/pyproject.toml orchestrator/pyproject.toml
# Optional system CA mount supports builds inside the managed Codex environment.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      SSL_CERT_FILE=/run/secrets/proxy_ca uv sync --frozen --all-packages --no-default-groups; \
    else uv sync --frozen --all-packages --no-default-groups; fi
COPY manager manager
COPY orchestrator orchestrator
COPY docs/source docs/source
