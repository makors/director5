"""Bounded runtime diagnostics and disposable, site-confined TTY sessions."""

import inspect
import queue
from unittest.mock import MagicMock

import docker.errors
import pytest
from docker.api.service import ServiceApiMixin
from starlette.websockets import WebSocketDisconnect

from orchestrator import settings
from orchestrator.api.runtime import services


def runtime_client(site_id=7):
    client = MagicMock()
    service = MagicMock(id="service-seven", name=f"site_{site_id:04d}")
    service.name = f"site_{site_id:04d}"
    service.attrs = {"Spec": {"Mode": {"Replicated": {"Replicas": 1}}}}
    client.services.list.return_value = [service]
    return client, service


def test_status_reports_real_task_state():
    client, service = runtime_client()
    service.tasks.return_value = [
        {"DesiredState": "running", "Status": {"State": "running"}},
        {"DesiredState": "shutdown", "Status": {"State": "failed"}},
    ]
    assert services.runtime_status(client, 7) == {"state": "running", "running": 1, "desired": 1}
    service.tasks.return_value = []
    service.attrs["Spec"]["Mode"]["Replicated"]["Replicas"] = 0
    assert services.runtime_status(client, 7)["state"] == "stopped"


def test_logs_are_bounded_and_stream_is_closed():
    client, service = runtime_client()
    stream = MagicMock()
    stream.__iter__.return_value = iter([b"a" * 50000, b"b" * 50000])
    service.logs.return_value = stream
    result = services.runtime_logs(client, 7, 200)
    assert len(result["output"]) == services.MAX_LOG_BYTES
    assert result["truncated"] is True
    stream.close.assert_called_once()
    assert service.logs.call_args.kwargs["follow"] is False
    # The Swarm API streams an iterator itself and does not accept container.logs'
    # stream parameter. Bind to the real SDK contract so the mock cannot mask this.
    inspect.signature(ServiceApiMixin.service_logs).bind(
        None, "service-seven", **service.logs.call_args.kwargs
    )


def test_shell_uses_cached_site_image_without_attaching_to_website(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SITES_DIR", tmp_path)
    monkeypatch.setattr(settings, "HOST_SITES_DIR", tmp_path)
    client, _ = runtime_client()
    session = services.TerminalSession(client, 7, services.TerminalOptions())
    params = client.containers.create.call_args.kwargs
    assert params["image"] == "site_0007"
    assert params["command"] == ["/bin/sh", "-c", "stty sane 2>/dev/null; exec /bin/sh -i"]
    client.containers.list.assert_not_called()
    client.api.exec_create.assert_not_called()
    session.close()


def test_helper_start_failure_removes_created_container(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SITES_DIR", tmp_path)
    monkeypatch.setattr(settings, "HOST_SITES_DIR", tmp_path)
    client, _ = runtime_client()
    client.containers.create.return_value.start.side_effect = docker.errors.APIError("Cannot start")
    with pytest.raises(docker.errors.APIError, match="Cannot start"):
        services.TerminalSession(client, 7, services.TerminalOptions())
    client.containers.create.return_value.remove.assert_called_once_with(force=True, v=True)


def test_shell_can_pull_approved_base_before_first_website_deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SITES_DIR", tmp_path)
    monkeypatch.setattr(settings, "HOST_SITES_DIR", tmp_path)
    client, _ = runtime_client()
    client.images.get.side_effect = docker.errors.ImageNotFound("Not cached")
    session = services.TerminalSession(
        client, 7, services.TerminalOptions(workspace_image="python:3.13-slim")
    )
    client.images.pull.assert_called_once_with("python:3.13-slim")
    assert client.containers.create.call_args.kwargs["image"] == "python:3.13-slim"
    session.close()


@pytest.mark.parametrize("kind", ("shell", "database"))
def test_disposable_workspace_and_database_helpers_are_isolated_and_removed(
    kind, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "SITES_DIR", tmp_path)
    monkeypatch.setattr(settings, "HOST_SITES_DIR", tmp_path)
    client, _ = runtime_client()
    client.containers.list.return_value = []
    helper = client.containers.create.return_value
    if kind == "database":
        options = services.TerminalOptions(
            kind=kind,
            command=["psql", "--username", "site_7"],
            environment={"PGPASSWORD": "site-secret"},
        )
    else:
        client.images.get.side_effect = [docker.errors.ImageNotFound("Not built"), MagicMock()]
        options = services.TerminalOptions()
    session = services.TerminalSession(client, 7, options)
    params = client.containers.create.call_args.kwargs
    assert params["image"] == ("postgres:17-alpine" if kind == "database" else "alpine:3.21")
    assert params["privileged"] is False
    assert params["read_only"] is True
    assert params["cap_drop"] == ["ALL"]
    assert params["pids_limit"] == 128
    assert params["mem_limit"] <= 2 * 1024**3
    assert params["mounts"][0]["Source"] == str(tmp_path / "00" / "07")
    assert params["mounts"][0]["Target"] == "/site"
    assert "site-secret" not in str(params["command"])
    if kind == "database":
        assert params["environment"]["PGPASSWORD"] == "site-secret"
    helper.start.assert_called_once()
    session.close()
    helper.remove.assert_called_once_with(force=True, v=True)
    client.api.exec_create.assert_not_called()


def test_helper_refuses_site_root_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SITES_DIR", tmp_path)
    monkeypatch.setattr(settings, "HOST_SITES_DIR", tmp_path)
    other = tmp_path / "00" / "08"
    other.mkdir(parents=True)
    (tmp_path / "00" / "07").symlink_to(other, target_is_directory=True)
    client, _ = runtime_client()
    client.containers.list.return_value = []
    with pytest.raises(OSError, match="Not a directory"):
        services.TerminalSession(client, 7, services.TerminalOptions())
    client.containers.create.assert_not_called()


def test_runtime_websocket_requires_internal_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "APPSERVER_TOKEN", "runtime-secret")
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/runtime/sites/7/terminal"),
    ):
        pass


def test_real_websocket_protocol_forwards_bytes_and_resize(client, monkeypatch):
    class Session:
        def __init__(self, *_):
            self.chunks = queue.Queue()
            self.chunks.put(b"site output\r\n")
            self.input = []
            self.sizes = []
            self.closed = False

        def read(self):
            try:
                return self.chunks.get(timeout=0.02)
            except queue.Empty:
                return None

        def write(self, value):
            self.input.append(value)

        def resize(self, rows, cols):
            self.sizes.append((rows, cols))

        def close(self):
            self.closed = True

    holder = []

    def make_session(*args):
        session = Session(*args)
        holder.append(session)
        return session

    monkeypatch.setattr(services, "docker_client", MagicMock())
    monkeypatch.setattr(services, "TerminalSession", make_session)
    with client.websocket_connect("/api/runtime/sites/7/terminal") as socket:
        socket.send_json({"kind": "shell"})
        assert socket.receive_json() == {"type": "ready"}
        assert socket.receive_bytes() == b"site output\r\n"
        socket.send_bytes(b"pwd\r")
        socket.send_json({"type": "resize", "rows": 30, "cols": 100})
        socket.send_json({"type": "unsupported"})
        assert socket.receive_json()["type"] == "error"
    assert holder[0].input == [b"pwd\r"]
    assert holder[0].sizes == [(30, 100)]
    assert holder[0].closed is True
