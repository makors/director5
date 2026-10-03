"""Settings for the orchestrator.

Usage:

    .. code-block:: pycon

        >>> from . import settings
        >>> settings.DEBUG
        True
"""

import os
from pathlib import Path

DEBUG = os.environ.get("DIRECTOR_DEBUG", "true").lower() == "true"
APPSERVER_TOKEN = os.environ.get("DIRECTOR_APPSERVER_TOKEN", "")

TIMEZONE = "America/New_York"

CI = "CI" in os.environ

SITES_DIR = Path("/data/sites")

if CI:
    SITES_DIR = Path("/tmp/sites")

if DEBUG and not CI:
    pwd = os.environ.get("PWD_HOST")
    if pwd is None:
        raise RuntimeError("Run scripts in the docker compose container!")
    compose_on_host = Path(pwd)
    repo_root_host = compose_on_host.parent.parent
    # note that this matches the bind mount in compose.yaml
    # we do this so that containers spawned on the host
    # still can access the site via a bind mount
    # TODO: this is hacky, our dev environment should not be
    # leaking onto production code.
    HOST_SITES_DIR = repo_root_host / "docker" / "storage" / "sites"
else:
    HOST_SITES_DIR = SITES_DIR

# Docker service configuration
TMP_TMPFS_SIZE = 10 * 1000 * 1000  # 10 MB
RUN_TMPFS_SIZE = 10 * 1000 * 100  # 10 MB

# Isolated terminal workspaces and SQL clients use site-scoped credentials.
RUNTIME_WORKSPACE_IMAGE = os.environ.get("RUNTIME_WORKSPACE_IMAGE", "alpine:3.21")
RUNTIME_POSTGRES_IMAGE = os.environ.get("RUNTIME_POSTGRES_IMAGE", "postgres:17-alpine")
RUNTIME_MYSQL_IMAGE = os.environ.get("RUNTIME_MYSQL_IMAGE", "mysql:8.4")
RUNTIME_HELPER_NETWORK = os.environ.get("RUNTIME_HELPER_NETWORK", "bridge")
