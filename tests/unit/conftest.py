"""Shared fixtures and mock helpers for unit tests."""

import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from k7.core.core import K7Core

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "src/k7/deploy"
# What the k7-api / k7-agent manifests set; read when k7.api.* is imported.
os.environ.update(K7_LOG_FORMAT="json", K7_LOG_LEVEL="info", K7_METRICS_PORT="off")


def install_tree_text() -> str:
    """The install playbook plus every role file it imports, as one string."""
    files = [DEPLOY_DIR / "k7-install-node.yaml", *sorted((DEPLOY_DIR / "roles").rglob("*.y*ml"))]
    files += sorted((DEPLOY_DIR / "roles").rglob("*.j2"))
    return "\n".join(f.read_text() for f in files)


FAKE_API_IMAGE = "ghcr.io/katakate/k7-api@sha256:" + "a" * 64
FAKE_INSTALLER_IMAGE = "ghcr.io/katakate/k7-node-installer@sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def packaged_images_lock(monkeypatch):
    """Behave like a release build, which packages images.lock.yaml."""
    lock = {"k7-api": FAKE_API_IMAGE, "k7-node-installer": FAKE_INSTALLER_IMAGE}
    monkeypatch.setattr("k7.deploy.images.load_lock", lambda: lock)


PLAYBOOK_SETTINGS = {
    "K7_SYSTEM_NAMESPACE": "kube-system",
    "K7_EGRESS_MODE": "full",
    "K7_KQL_STORAGE_CLASS": "longhorn",
    "K7_KQL_SNAPSHOT_CLASS": "longhorn",
    "K7_API_MODE": "development",
}


@pytest.fixture(autouse=True)
def playbook_cluster_settings(monkeypatch):
    """The k7.core.cluster_config values a `k7 install` cluster has."""
    for key, value in PLAYBOOK_SETTINGS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("K7_KUBECONFIG", raising=False)


METERING = {
    "sampleInterval": 15,
    "storageInterval": 60,
    "spool": {"maxBytes": 2**30, "highWaterPercent": 80},
    "requireDelivery": False,
    "prometheusUrl": None,
    "sinks": [],
}
CLUSTER_ID = "8f0c2a4e-1b7d-4c55-9a3e-2f6b0d1c9e77"


@pytest.fixture(autouse=True)
def metering_env(monkeypatch, tmp_path_factory):
    """What the manifests mount: the k7-metering ConfigMap, the cluster id, a writable spool dir."""
    tmp_path = tmp_path_factory.mktemp("metering")
    cfg = tmp_path / "metering.json"
    cfg.write_text(json.dumps(METERING))
    monkeypatch.setenv("K7_METERING_CONFIG", str(cfg))
    monkeypatch.setenv("K7_USAGE_SPOOL_DIR", str(tmp_path / "usage-spool"))
    monkeypatch.setenv("K7_CLUSTER_ID", CLUSTER_ID)
    monkeypatch.setenv("K7_NODE_NAME", "node-a")
    return cfg


@pytest.fixture(autouse=True)
def restore_logging():
    """CLI tests run k7.logging.configure("human"), which stops k7.* records reaching caplog."""
    loggers = [logging.getLogger(n) for n in ("", "k7", "k7.op")]
    saved = [(lg, lg.level, lg.propagate, list(lg.handlers)) for lg in loggers]
    yield
    for lg, level, propagate, handlers in saved:
        lg.setLevel(level)
        lg.propagate = propagate
        lg.handlers[:] = handlers


@pytest.fixture(autouse=True)
def no_tenants(monkeypatch):
    """API routes look up the namespace's Tenant CR on every request; unit tests have no cluster."""
    monkeypatch.setattr("k7.api.main.list_tenant_crs", AsyncMock(return_value=[]))


@pytest.fixture()
def core() -> K7Core:
    """K7Core with k8s config loading disabled."""
    with patch.object(K7Core, "_load_k3s_config", new_callable=AsyncMock):
        c = K7Core()
        c._config_loaded = True
        return c


def mock_deployment(
    name: str = "test-sb",
    namespace: str = "default",
    runtime_class: str = "kata",
    annotations: dict | None = None,
    labels: dict | None = None,
):
    dep = MagicMock()
    dep.metadata.name = name
    dep.metadata.namespace = namespace
    dep.metadata.annotations = annotations or {}
    dep.metadata.labels = labels or {"runtime": "kata"}
    dep.spec.template.spec.runtime_class_name = runtime_class
    return dep


def mock_pod(
    name: str = "test-sb-xyz",
    namespace: str = "default",
    phase: str = "Running",
    image: str = "alpine:3.20",
    ready: bool = True,
    restarts: int = 0,
):
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.creation_timestamp = None
    pod.metadata.deletion_timestamp = None
    pod.status.phase = phase

    condition = MagicMock()
    condition.type = "Ready"
    condition.status = "True" if ready else "False"
    pod.status.conditions = [condition]

    cs = MagicMock()
    cs.restart_count = restarts
    pod.status.container_statuses = [cs]

    container = MagicMock()
    container.image = image
    container.resources.limits = {"cpu": "500m", "memory": "256Mi"}
    pod.spec.containers = [container]

    return pod
