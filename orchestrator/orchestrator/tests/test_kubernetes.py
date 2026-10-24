import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import docker.errors
import pytest
from fastapi import HTTPException
from kubernetes.client.exceptions import ApiException

from orchestrator import settings
from orchestrator.api.docker import gateway
from orchestrator.api.docker import router as docker_router
from orchestrator.api.docker.schema import SiteInfo
from orchestrator.api.kubernetes import cluster, manifests, runtime, sites
from orchestrator.api.runtime import router as runtime_router
from orchestrator.api.runtime.services import RuntimeUnavailable, TerminalOptions

DIGEST = "127.0.0.1:5000/site_0012@sha256:" + "a" * 64


def make_site(**overrides) -> SiteInfo:
    values = {
        "pk": 12,
        "hosts": ["robotics.example.test"],
        "is_served": True,
        "type_": "dynamic",
        "resource_limits": {"cpus": 0.5, "memory": "256MiB", "max_request_body_size": 1024},
        "db": {
            "url": "postgres://db.internal:5432/site_12",
            "name": "site_12",
            "username": "site_12",
            "password": "secret",
        },
    }
    return SiteInfo.model_validate(values | overrides)


def container(deployment):
    return deployment["spec"]["template"]["spec"]["containers"][0]


def test_dynamic_site_runs_pinned_image_in_kata_vm():
    deployment = manifests.site_deployment(make_site(), DIGEST, "now")
    pod = deployment["spec"]["template"]["spec"]
    site = container(deployment)

    assert deployment["metadata"]["name"] == "site-0012"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert pod["runtimeClassName"] == "kata"
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert site["image"] == DIGEST
    assert site["command"][:2] == ["sh", "-c"]
    assert "/site/run.sh" in site["command"][2]
    assert site["securityContext"]["readOnlyRootFilesystem"] is True
    assert site["resources"]["limits"] == {"cpu": "500m", "memory": str(256 * 1024**2)}
    env = {item["name"]: item["value"] for item in site["env"]}
    assert env["PORT"] == "80"
    assert env["DIRECTOR_DATABASE_PASSWORD"] == "secret"
    assert {"name": "site", "mountPath": "/root", "subPath": ".home"} in site["volumeMounts"]
    assert pod["volumes"][0]["hostPath"]["path"].endswith("00/12")


def test_static_site_serves_public_files_read_only():
    deployment = manifests.site_deployment(
        make_site(type_="static", is_served=False, db=None), "unused", "now"
    )
    site = container(deployment)

    assert deployment["spec"]["replicas"] == 0
    assert site["image"] == settings.STATIC_SITE_IMAGE
    assert "command" not in site
    assert {
        "name": "site",
        "mountPath": "/usr/share/nginx/html",
        "subPath": "public",
        "readOnly": True,
    } in site["volumeMounts"]


def test_runtime_class_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "KUBERNETES_RUNTIME_CLASS", "")
    pod = manifests.site_deployment(make_site(), DIGEST, "now")["spec"]["template"]["spec"]
    assert "runtimeClassName" not in pod


def test_routes_match_swarm_rules_and_strip_directory_prefixes():
    site = make_site(
        routes=[
            {"host": "robotics.example.test"},
            {"host": "activities.example.test", "path_prefix": "/robotics/"},
        ]
    )
    route = manifests.ingress_route(site, through_gateway=False)
    middlewares = {item["metadata"]["name"]: item["spec"] for item in manifests.middlewares(site)}

    assert route["spec"]["entryPoints"] == ["websecure"]
    first, second = route["spec"]["routes"]
    assert first["match"] == "Host(`robotics.example.test`)"
    assert first["middlewares"] == [{"name": "site-0012-body"}]
    assert second["match"] == (
        "Host(`activities.example.test`) && (Path(`/robotics`) || PathPrefix(`/robotics/`))"
    )
    assert second["middlewares"][1] == {"name": "site-0012-1-strip"}
    assert second["services"] == [{"name": "site-0012", "port": 80}]
    assert middlewares["site-0012-body"] == {"buffering": {"maxRequestBodyBytes": 1024}}
    assert middlewares["site-0012-1-strip"] == {"stripPrefix": {"prefixes": ["/robotics"]}}


def test_terminal_pod_is_unprivileged_and_confined_to_site():
    pod = manifests.terminal_pod(
        12,
        "site-0012-term-abcd",
        "alpine:3.21",
        ["/bin/sh"],
        {"TZ": "UTC"},
        host_dir="/data/sites/00/12",
        uid=1000,
        gid=1000,
        memory=256 * 1024**2,
        nano_cpus=500_000_000,
    )
    spec = pod["spec"]
    terminal = spec["containers"][0]

    assert spec["runtimeClassName"] == "kata"
    assert spec["restartPolicy"] == "Never"
    assert spec["securityContext"] == {"runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": True}
    assert terminal["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert terminal["securityContext"]["allowPrivilegeEscalation"] is False
    assert terminal["tty"] is True
    assert spec["volumes"][0] == {
        "name": "site",
        "hostPath": {"path": "/data/sites/00/12", "type": "Directory"},
    }


def docker_client_with_image(digests):
    client = Mock()
    client.images.get.return_value.attrs = {"RepoDigests": digests}
    client.services.list.return_value = []
    return client


def test_deploy_pins_pushed_digest_and_retires_swarm_service(monkeypatch):
    applied = []
    monkeypatch.setattr(cluster, "apply_site", lambda objects, site_id: applied.extend(objects))
    client = docker_client_with_image([DIGEST])
    swarm = Mock()
    swarm.name = "site_0012"
    client.services.list.side_effect = (
        lambda filters: [swarm] if filters["name"] == "site_0012" else []
    )

    sites.deploy_site(client, make_site())

    kinds = [(item["kind"], item["metadata"]["name"]) for item in applied]
    assert kinds == [
        ("Deployment", "site-0012"),
        ("Service", "site-0012"),
        ("Middleware", "site-0012-body"),
        ("IngressRoute", "site-0012"),
    ]
    assert container(applied[0])["image"] == DIGEST
    swarm.remove.assert_called_once()


def test_dynamic_site_needs_a_pushed_image(monkeypatch):
    monkeypatch.setattr(cluster, "apply_site", Mock())
    client = Mock()
    client.images.get.side_effect = docker.errors.ImageNotFound("missing")

    with pytest.raises(sites.ImageUnavailable):
        sites.deploy_site(client, make_site())
    cluster.apply_site.assert_not_called()


def test_custom_nginx_routes_dynamic_site_through_proxy(monkeypatch):
    applied = []
    monkeypatch.setattr(cluster, "apply_site", lambda objects, site_id: applied.extend(objects))
    client = docker_client_with_image([DIGEST])
    site = make_site(custom_nginx_config="add_header X-Test yes;")
    monkeypatch.setattr(settings, "SITE_RUNTIME", "kubernetes")

    sites.deploy_site(client, site)

    names = [(item["kind"], item["metadata"]["name"]) for item in applied]
    assert ("Deployment", "site-0012-proxy") in names
    assert ("Service", "site-0012-proxy") in names
    route = next(item for item in applied if item["kind"] == "IngressRoute")
    assert route["spec"]["routes"][0]["services"] == [{"name": "site-0012-proxy", "port": 80}]
    config = gateway.config_path(site).read_text()
    assert "resolver 10.43.0.10" in config
    assert "http://site-0012.director-sites.svc.cluster.local:80" in config


def test_static_custom_nginx_config_mounts_into_site(monkeypatch):
    applied = []
    monkeypatch.setattr(cluster, "apply_site", lambda objects, site_id: applied.extend(objects))
    site = make_site(type_="static", db=None, custom_nginx_config="add_header X-Test yes;")

    sites.deploy_site(docker_client_with_image([]), site)

    deployment = applied[0]
    mounts = container(deployment)["volumeMounts"]
    assert any(mount["mountPath"] == "/etc/nginx/conf.d/default.conf" for mount in mounts)
    assert not any(item["metadata"]["name"].endswith("-proxy") for item in applied)


def test_push_reports_registry_errors():
    client = Mock()
    client.images.push.return_value = iter([{"status": "Pushing"}, {"error": "denied"}])
    with pytest.raises(docker.errors.APIError):
        sites.push_image(client, make_site())
    client.images.get.return_value.tag.assert_called_once_with("127.0.0.1:5000/site_0012", "latest")


def test_router_uses_kubernetes_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "SITE_RUNTIME", "kubernetes")
    monkeypatch.setattr(docker_router.docker, "from_env", Mock)
    deploy = Mock()
    monkeypatch.setattr(docker_router.kubernetes_sites, "deploy_site", deploy)

    assert docker_router.update_docker_service(make_site()) == {}
    deploy.assert_called_once()

    deploy.side_effect = sites.ImageUnavailable("Build the site's image before deploying it.")
    with pytest.raises(HTTPException) as error:
        docker_router.update_docker_service(make_site())
    assert error.value.status_code == 400
    assert error.value.detail["user_error"] is True


def test_build_pushes_image_for_kubernetes(site_info, monkeypatch):
    monkeypatch.setattr(settings, "SITE_RUNTIME", "kubernetes")
    client = Mock()
    client.images.build.return_value = (Mock(), iter([]))
    monkeypatch.setattr(docker_router.docker, "from_env", lambda: client)
    push = Mock()
    monkeypatch.setattr(docker_router.kubernetes_sites, "push_image", push)

    docker_router.build_image(site_info)
    push.assert_called_once_with(client, site_info)


def api_error(status):
    return ApiException(status=status, reason="error")


def test_upsert_creates_then_replaces_keeping_version_and_cluster_ip():
    created, replaced = Mock(), Mock()
    read = Mock(side_effect=api_error(404))
    body = manifests.service(12)
    cluster._upsert(body, read, created, replaced, name="site-0012", namespace="director-sites")
    created.assert_called_once_with(body=body, namespace="director-sites")

    current = SimpleNamespace(
        metadata=SimpleNamespace(resource_version="41"),
        spec=SimpleNamespace(cluster_ip="10.43.0.20"),
    )
    body = manifests.service(12)
    cluster._upsert(
        body, Mock(return_value=current), created, replaced, name="site-0012", namespace="ns"
    )
    sent = replaced.call_args.kwargs["body"]
    assert sent["metadata"]["resourceVersion"] == "41"
    assert sent["spec"]["clusterIP"] == "10.43.0.20"

    with pytest.raises(ApiException):
        cluster._upsert(body, Mock(side_effect=api_error(403)), created, replaced, name="x")


def test_apply_site_removes_objects_no_longer_wanted(monkeypatch):
    monkeypatch.setattr(cluster, "apply", Mock())
    remove = Mock()
    monkeypatch.setattr(cluster, "remove_site_objects", remove)
    objects = [manifests.service(12), manifests.ingress_route(make_site(), through_gateway=False)]

    cluster.apply_site(objects, 12)
    assert remove.call_args.kwargs["keep"] == {
        ("Service", "site-0012"),
        ("IngressRoute", "site-0012"),
    }


def pod(name, phase="Running", *, running=True, created=0):
    state = SimpleNamespace(running=object() if running else None, waiting=None)
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            deletion_timestamp=None,
            creation_timestamp=datetime.fromtimestamp(created, UTC),
        ),
        status=SimpleNamespace(phase=phase, container_statuses=[SimpleNamespace(state=state)]),
    )


@pytest.fixture
def fake_cluster(monkeypatch):
    apps, core = MagicMock(), MagicMock()
    monkeypatch.setattr(cluster, "apps", lambda: apps)
    monkeypatch.setattr(cluster, "core", lambda: core)
    apps.read_namespaced_deployment.return_value = SimpleNamespace(spec=SimpleNamespace(replicas=1))
    return SimpleNamespace(apps=apps, core=core)


def test_status_counts_running_pods(fake_cluster):
    fake_cluster.core.list_namespaced_pod.return_value.items = []
    assert runtime.runtime_status(None, 12) == {"state": "starting", "running": 0, "desired": 1}

    fake_cluster.core.list_namespaced_pod.return_value.items = [pod("a")]
    assert runtime.runtime_status(None, 12)["state"] == "running"

    fake_cluster.apps.read_namespaced_deployment.side_effect = api_error(404)
    with pytest.raises(RuntimeUnavailable):
        runtime.runtime_status(None, 12)


def test_logs_read_newest_pod_and_are_bounded(fake_cluster):
    fake_cluster.core.list_namespaced_pod.return_value.items = [
        pod("new", created=20),
        pod("old", created=10),
    ]
    fake_cluster.core.read_namespaced_pod_log.return_value = "x" * (70 * 1024)

    result = runtime.runtime_logs(None, 12, tail=50)
    args = fake_cluster.core.read_namespaced_pod_log.call_args
    assert args.args[0] == "new"
    assert args.kwargs["tail_lines"] == 50
    assert result["truncated"] is True
    assert len(result["output"]) == 64 * 1024


def test_terminal_pod_is_attached_then_deleted(tmp_path, fake_cluster, monkeypatch):
    (tmp_path / "00" / "12").mkdir(parents=True)
    fake_cluster.core.read_namespaced_pod.return_value = pod("t")
    connection = MagicMock()
    connection.is_open.return_value = True
    connection.read_stdout.return_value = b"$ "
    connection.read_stderr.return_value = b""
    attach = Mock(return_value=connection)
    monkeypatch.setattr(runtime, "stream", attach)
    docker_client = Mock()
    docker_client.images.get.side_effect = docker.errors.ImageNotFound("none")

    session = runtime.TerminalSession(docker_client, 12, TerminalOptions())
    created = fake_cluster.core.create_namespaced_pod.call_args.args[1]
    name = created["metadata"]["name"]
    assert name.startswith("site-0012-term-")
    assert created["spec"]["containers"][0]["image"] == settings.RUNTIME_WORKSPACE_IMAGE
    assert attach.call_args.kwargs["tty"] is True
    assert attach.call_args.kwargs["binary"] is True
    assert session.read() == b"$ "
    session.write(b"ls\n")
    connection.write_stdin.assert_called_with(b"ls\n")

    session.close()
    connection.close.assert_called_once()
    fake_cluster.core.delete_namespaced_pod.assert_called_once_with(
        name, "director-sites", grace_period_seconds=0
    )


def test_terminal_that_cannot_start_is_cleaned_up(tmp_path, fake_cluster, monkeypatch):
    (tmp_path / "00" / "12").mkdir(parents=True)
    failed = pod("t", phase="Pending", running=False)
    failed.status.container_statuses[0].state.waiting = SimpleNamespace(reason="ImagePullBackOff")
    fake_cluster.core.read_namespaced_pod.return_value = failed
    docker_client = Mock()
    docker_client.images.get.side_effect = docker.errors.ImageNotFound("none")

    with pytest.raises(RuntimeUnavailable):
        runtime.TerminalSession(docker_client, 12, TerminalOptions())
    fake_cluster.core.delete_namespaced_pod.assert_called_once()


def test_runtime_router_selects_backend(monkeypatch):
    assert runtime_router.backend() is runtime_router.services
    monkeypatch.setattr(settings, "SITE_RUNTIME", "kubernetes")
    assert runtime_router.backend() is runtime


@pytest.mark.parametrize(
    ("build", "model"),
    (
        (lambda: manifests.site_deployment(make_site(), DIGEST, "now"), "V1Deployment"),
        (
            lambda: manifests.site_deployment(
                make_site(type_="static", db=None),
                "",
                "now",
                "/data/director-runtime-config/12.conf",
            ),
            "V1Deployment",
        ),
        (
            lambda: manifests.gateway_deployment(make_site(), "/data/x.conf", "now"),
            "V1Deployment",
        ),
        (lambda: manifests.service(12), "V1Service"),
        (
            lambda: manifests.terminal_pod(
                12,
                "site-0012-term-a",
                "alpine",
                ["sh"],
                {},
                host_dir="/data",
                uid=0,
                gid=0,
                memory=1,
                nano_cpus=1,
            ),
            "V1Pod",
        ),
    ),
)
def test_manifests_use_only_real_kubernetes_fields(build, model):
    """Unknown or misplaced fields are dropped by the typed models, so a round trip shows them."""
    from kubernetes.client import ApiClient

    body = build()
    api = ApiClient()
    response = SimpleNamespace(data=json.dumps(body))
    typed = api.deserialize(response, model)
    assert api.sanitize_for_serialization(typed) == body
