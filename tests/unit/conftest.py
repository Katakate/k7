"""Shared fixtures and mock helpers for unit tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from k7.core.core import K7Core


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
    image: str = "alpine:latest",
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
