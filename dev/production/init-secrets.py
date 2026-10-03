#!/usr/bin/env python3
"""Create credentials on the destination server without printing them."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent
target = root / ".env"
lines = (root / ".env.example").read_text().splitlines()
generated = {
    "DIRECTOR_SECRET_KEY",
    "DIRECTOR_APPSERVER_TOKEN",
    "DIRECTOR_POSTGRES_PASSWORD",
    "DIRECTOR_SITE_POSTGRES_PASSWORD",
    "DIRECTOR_MYSQL_ROOT_PASSWORD",
}
output = []
for line in lines:
    key, separator, value = line.partition("=")
    if separator and key in generated:
        line = key + "=" + secrets.token_urlsafe(48)
    output.append(line)
try:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit("Existing .env retained. No credentials were changed.") from None
with os.fdopen(fd, "w") as handle:
    handle.write("\n".join(output) + "\n")
print("Created private .env. Configure the Ion OAuth client and administrator identity before starting.")
