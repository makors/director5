"""Create, update, inspect and remove site objects through the Kubernetes API."""

import functools
from collections.abc import Callable
from typing import Any

from kubernetes import client as k8s
from kubernetes import config
from kubernetes.client.exceptions import ApiException

from orchestrator import settings

from . import manifests


@functools.cache
def api_client() -> k8s.ApiClient:
    """In-cluster credentials when running in a pod, otherwise ``$KUBECONFIG``."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return k8s.ApiClient()


def core() -> k8s.CoreV1Api:
    return k8s.CoreV1Api(api_client())


def apps() -> k8s.AppsV1Api:
    return k8s.AppsV1Api(api_client())


def custom() -> k8s.CustomObjectsApi:
    return k8s.CustomObjectsApi(api_client())


def missing(error: ApiException) -> bool:
    return error.status == 404


def _upsert(
    body: dict[str, Any],
    read: Callable[..., Any],
    create: Callable[..., Any],
    replace: Callable[..., Any],
    **target: Any,
) -> None:
    """Create the object, or replace it in full so removed fields do not linger."""
    try:
        current = read(**target)
    except ApiException as error:
        if not missing(error):
            raise
        create(body=body, **{k: v for k, v in target.items() if k != "name"})
        return
    version = (
        current["metadata"]["resourceVersion"]
        if isinstance(current, dict)
        else current.metadata.resource_version
    )
    body["metadata"]["resourceVersion"] = version
    if body["kind"] == "Service":
        # A Service's cluster IP is immutable once assigned.
        body["spec"]["clusterIP"] = current.spec.cluster_ip
    replace(body=body, **target)


def _delete(delete: Callable[..., Any], **target: Any) -> None:
    try:
        delete(**target)
    except ApiException as error:
        if not missing(error):
            raise


def apply(body: dict[str, Any]) -> None:
    namespace = body["metadata"]["namespace"]
    name = body["metadata"]["name"]
    kind = body["kind"]
    if kind == "Deployment":
        api = apps()
        _upsert(
            body,
            api.read_namespaced_deployment,
            api.create_namespaced_deployment,
            api.replace_namespaced_deployment,
            name=name,
            namespace=namespace,
        )
    elif kind == "Service":
        api = core()
        _upsert(
            body,
            api.read_namespaced_service,
            api.create_namespaced_service,
            api.replace_namespaced_service,
            name=name,
            namespace=namespace,
        )
    else:
        api = custom()
        group, version = manifests.TRAEFIK_API_VERSION.split("/")
        plural = {"IngressRoute": "ingressroutes", "Middleware": "middlewares"}[kind]
        crd = {"group": group, "version": version, "plural": plural, "namespace": namespace}
        _upsert(
            body,
            functools.partial(api.get_namespaced_custom_object, **crd),
            functools.partial(api.create_namespaced_custom_object, **crd),
            functools.partial(api.replace_namespaced_custom_object, **crd),
            name=name,
        )


def remove_site_objects(site_id: int, *, keep: set[tuple[str, str]] | None = None) -> None:
    """Delete this site's objects, except the (kind, name) pairs in ``keep``."""
    keep = keep or set()
    namespace = settings.KUBERNETES_NAMESPACE
    labels = f"director.site/id={site_id},app.kubernetes.io/managed-by=director"
    apps_api, core_api, custom_api = apps(), core(), custom()
    for deployment in apps_api.list_namespaced_deployment(namespace, label_selector=labels).items:
        if ("Deployment", deployment.metadata.name) not in keep:
            _delete(
                apps_api.delete_namespaced_deployment,
                name=deployment.metadata.name,
                namespace=namespace,
            )
    for item in core_api.list_namespaced_service(namespace, label_selector=labels).items:
        if ("Service", item.metadata.name) not in keep:
            _delete(
                core_api.delete_namespaced_service, name=item.metadata.name, namespace=namespace
            )
    group, version = manifests.TRAEFIK_API_VERSION.split("/")
    for kind, plural in (("IngressRoute", "ingressroutes"), ("Middleware", "middlewares")):
        found = custom_api.list_namespaced_custom_object(
            group, version, namespace, plural, label_selector=labels
        )
        for item in found.get("items", []):
            name = item["metadata"]["name"]
            if (kind, name) not in keep:
                _delete(
                    custom_api.delete_namespaced_custom_object,
                    group=group,
                    version=version,
                    namespace=namespace,
                    plural=plural,
                    name=name,
                )


def apply_site(objects: list[dict[str, Any]], site_id: int) -> None:
    """Apply the full desired set, then remove anything left from an earlier shape."""
    for body in objects:
        apply(body)
    remove_site_objects(
        site_id, keep={(body["kind"], body["metadata"]["name"]) for body in objects}
    )
