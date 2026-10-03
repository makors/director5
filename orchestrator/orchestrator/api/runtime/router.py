"""Bounded diagnostics and an authenticated site-container terminal."""

import asyncio
import contextlib
import json

import docker.errors
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from orchestrator.security import credential_valid

from . import services

router = APIRouter()
MAX_LIFETIME = 3600


class RuntimeRequest(BaseModel):
    site_id: int = Field(ge=1)
    tail: int = Field(default=200, ge=1, le=500)


def inspect_runtime(data, callback):
    client = None
    try:
        client = services.docker_client()
        return callback(client, data.site_id)
    except services.RuntimeUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except docker.errors.DockerException as error:
        raise HTTPException(status_code=503, detail="The site's runtime is unavailable.") from error
    finally:
        if client is not None:
            client.close()


@router.post("/status")
def status(data: RuntimeRequest):
    return inspect_runtime(data, services.runtime_status)


@router.post("/logs")
def logs(data: RuntimeRequest):
    return inspect_runtime(
        data, lambda client, site_id: services.runtime_logs(client, site_id, data.tail)
    )


async def forward_terminal_output(websocket, session):
    while True:
        chunk = await asyncio.to_thread(session.read)
        if chunk == b"":
            return
        if chunk:
            await websocket.send_bytes(chunk)


async def forward_terminal_input(websocket, session):
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
            await asyncio.to_thread(session.write, data)
        elif message.get("text"):
            text = message["text"]
            if len(text) > 1024:
                raise ValueError("Terminal control message is too large")
            control = json.loads(text)
            if control.get("type") != "resize":
                raise ValueError("Unknown terminal control")
            rows, cols = control.get("rows"), control.get("cols")
            if type(rows) is not int or type(cols) is not int:
                raise ValueError("Invalid terminal size")
            await asyncio.to_thread(session.resize, rows, cols)


async def wait_for_terminal_tasks(tasks):
    await asyncio.wait(tasks, timeout=MAX_LIFETIME, return_when=asyncio.FIRST_COMPLETED)
    for task in tasks:
        if task.done() and not task.cancelled():
            task.result()


@router.websocket("/sites/{site_id}/terminal")
async def terminal(websocket: WebSocket, site_id: int):
    if not credential_valid(websocket.headers.get("authorization")) or site_id < 1:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    client = None
    session = None
    tasks = []
    try:
        raw_options = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        if len(raw_options) > 16384:
            raise ValueError("Terminal options are too large")
        options = services.TerminalOptions.model_validate_json(raw_options)
        client = await asyncio.to_thread(services.docker_client)
        session = await asyncio.to_thread(services.TerminalSession, client, site_id, options)
        await websocket.send_json({"type": "ready"})

        tasks = [
            asyncio.create_task(forward_terminal_output(websocket, session)),
            asyncio.create_task(forward_terminal_input(websocket, session)),
        ]
        await wait_for_terminal_tasks(tasks)
    except (
        ValueError,
        ValidationError,
        TimeoutError,
        services.RuntimeUnavailable,
        docker.errors.DockerException,
        OSError,
    ):
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "This site's terminal is unavailable. Check that its workspace or database client image is available on the appserver.",
                }
            )
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        if session is not None:
            await asyncio.to_thread(session.close)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if client is not None:
            await asyncio.to_thread(client.close)
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await websocket.close()
