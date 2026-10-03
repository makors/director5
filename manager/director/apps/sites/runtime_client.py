"""Connect to trusted appservers without accepting a client-selected host."""

import asyncio
import inspect
import json
import ssl

import requests
from django.conf import settings
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .appserver import Appserver


class RuntimeUnavailable(Exception):  # noqa: N818
    pass


def ssl_options():
    config = settings.DIRECTOR_APPSERVER_SSL
    if not config:
        return {}
    options = {"verify": str(config["cafile"])}
    certificate = config.get("client_cert")
    if certificate:
        options["cert"] = (str(certificate["certfile"]), str(certificate["keyfile"]))
    return options


def websocket_ssl():
    config = settings.DIRECTOR_APPSERVER_SSL
    if not config:
        return None
    context = ssl.create_default_context(cafile=str(config["cafile"]))
    certificate = config.get("client_cert")
    if certificate:
        context.load_cert_chain(
            str(certificate["certfile"]), str(certificate["keyfile"]), certificate.get("password")
        )
    return context


def runtime_request(site_id, endpoint, tail=200):
    if endpoint not in {"status", "logs"}:
        raise ValueError("Unknown runtime endpoint")
    missing = False
    for host in settings.DIRECTOR_APPSERVER_HOSTS:
        try:
            response = requests.post(
                f"{Appserver.protocol()}://{host}/api/runtime/{endpoint}",
                json={"site_id": site_id, "tail": tail},
                headers=Appserver.auth_headers(),
                timeout=(2, 5),
                **ssl_options(),
            )
            if response.status_code == 404:
                missing = True
                continue
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                continue
            return result
        except (requests.RequestException, ValueError):
            continue
    if missing:
        raise RuntimeUnavailable("This site has no deployed service.")
    raise RuntimeUnavailable("The site's runtime is unavailable. Please try again later.")


async def open_runtime_terminal(site_id, options):
    for host in settings.DIRECTOR_APPSERVER_HOSTS:
        socket = None
        try:
            connection_options = {
                "additional_headers": Appserver.auth_headers(),
                "open_timeout": 5,
                "close_timeout": 2,
                "max_size": 65536,
                "max_queue": 8,
            }
            # websockets 15 added automatic proxy discovery. Appservers are a
            # private deployment transport and must use a direct connection.
            if "proxy" in inspect.signature(connect).parameters:
                connection_options["proxy"] = None
            scheme = "wss" if settings.DIRECTOR_APPSERVER_SSL else "ws"
            if scheme == "wss":
                connection_options["ssl"] = websocket_ssl()
            socket = await connect(
                f"{scheme}://{host}/api/runtime/sites/{site_id}/terminal", **connection_options
            )
            await socket.send(json.dumps(options))
            ready = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            if isinstance(ready, dict) and ready.get("type") == "ready":
                return socket
        except (OSError, TimeoutError, ValueError, WebSocketException):
            pass
        if socket is not None:
            await socket.close()
    raise RuntimeUnavailable("The site's workspace or database terminal is unavailable.")
