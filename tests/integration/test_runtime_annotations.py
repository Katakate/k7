"""Admission policy k7-runtime-annotations on a live cluster: a tenant or
namespace admin cannot set the k7d / Kata runtime annotations, and containerd
forwards only the ones k7 sets. k7's own paths (create, docker, fork through
k7-api) are covered by test_k7d_api, test_docker, test_k7d and
test_firecracker, which run with the policy installed."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from .cluster import KUBECTL, kubectl

pytestmark = pytest.mark.integration

POLICY = "k7-runtime-annotations"
RESERVED = [
    {"k7d.katakate.org/cluster-id": "c1"},
    {"k7d.katakate.org/fork-source-vm": "x", "k7d.katakate.org/fork-source-cluster": "x"},
    {"io.katacontainers.config_path": "/opt/kata/share/defaults/kata-containers/configuration.toml"},
    {"io.katacontainers.config.runtime.disable_guest_seccomp": "true"},
]


def _apply(obj: dict, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*KUBECTL, "apply", *args, "-f", "-"], input=json.dumps(obj), capture_output=True, text=True)


def _pod(annotations: dict[str, str], runtime_class: str | None = None) -> dict:
    spec: dict = {"containers": [{"name": "c", "image": "alpine:3.20", "command": ["sleep", "3600"]}]}
    if runtime_class:
        spec["runtimeClassName"] = runtime_class
    return {"metadata": {"labels": {"app": "ann"}, "annotations": annotations}, "spec": spec}


def _workload(kind: str, ns: str, annotations: dict[str, str], name: str = "ann") -> dict:
    pod = _pod(annotations)
    api = {"Pod": "v1", "Job": "batch/v1", "CronJob": "batch/v1"}.get(kind, "apps/v1")
    meta = {"name": name, "namespace": ns}
    if kind == "Pod":
        return {"apiVersion": api, "kind": kind, "metadata": {**meta, **pod["metadata"]}, "spec": pod["spec"]}
    if kind == "Job":
        pod["spec"]["restartPolicy"] = "Never"
        return {"apiVersion": api, "kind": kind, "metadata": meta, "spec": {"template": pod}}
    if kind == "CronJob":
        pod["spec"]["restartPolicy"] = "Never"
        job = {"spec": {"template": pod}}
        return {
            "apiVersion": api,
            "kind": kind,
            "metadata": meta,
            "spec": {"schedule": "0 0 1 1 *", "jobTemplate": job},
        }
    sel = {"selector": {"matchLabels": {"app": "ann"}}, "template": pod}
    if kind == "StatefulSet":
        sel["serviceName"] = name
    if kind in ("Deployment", "StatefulSet"):
        sel["replicas"] = 0
    return {"apiVersion": api, "kind": kind, "metadata": meta, "spec": sel}


@pytest.fixture(params=["serviceaccount", "user"])
def tenant(request, test_namespace: str) -> list[str]:
    """kubectl --as flags for a namespace admin (ClusterRole admin, bound in the namespace only)."""
    ns = test_namespace
    if request.param == "serviceaccount":
        kubectl("-n", ns, "create", "serviceaccount", "dev", check=True)
        subject, who = f"--serviceaccount={ns}:dev", f"system:serviceaccount:{ns}:dev"
    else:
        subject, who = "--user=alice", "alice"
    kubectl("-n", ns, "create", "rolebinding", "dev-admin", "--clusterrole=admin", subject, check=True)
    return [f"--as={who}", "--as-group=system:authenticated"]


def test_policy_installed_and_type_checks_clean():
    vap = {}
    for _ in range(30):
        vap = json.loads(kubectl("get", "validatingadmissionpolicy", POLICY, "-o", "json", check=True).stdout)
        if vap.get("status", {}).get("observedGeneration") == vap["metadata"]["generation"]:
            break
        time.sleep(2)
    assert not vap["status"].get("typeChecking", {}).get("expressionWarnings"), vap["status"]
    binding = json.loads(kubectl("get", "validatingadmissionpolicybinding", POLICY, "-o", "json", check=True).stdout)
    assert binding["spec"]["validationActions"] == ["Deny"]


@pytest.mark.parametrize("kind", ["Deployment", "Pod", "ReplicaSet", "StatefulSet", "DaemonSet", "Job", "CronJob"])
def test_tenant_cannot_set_runtime_annotations(tenant, test_namespace, kind):
    for ann in RESERVED:
        r = _apply(_workload(kind, test_namespace, ann), "--dry-run=server", *tenant)
        assert r.returncode != 0 and POLICY in r.stderr, (ann, r.stdout, r.stderr)
    r = _apply(_workload(kind, test_namespace, {"team": "a"}), "--dry-run=server", *tenant)
    assert r.returncode == 0, r.stderr


def test_tenant_keeps_but_cannot_change_k7_annotations(tenant, test_namespace):
    """A Deployment k7 stamped (admin here) stays editable by the tenant, minus the reserved keys."""
    k7 = {"k7d.katakate.org/docker": "true"}
    assert _apply(_workload("Deployment", test_namespace, k7)).returncode == 0
    dep = ["-n", test_namespace, "deployment/ann"]
    assert kubectl("annotate", *dep, "team=a", *tenant).returncode == 0
    r = kubectl("rollout", "restart", *dep, *tenant)
    assert r.returncode == 0, r.stderr
    for patch in (
        {"k7d.katakate.org/docker": "false"},
        {"k7d.katakate.org/docker": None},
        {"k7d.katakate.org/cluster-id": "c1"},
    ):
        body = json.dumps({"spec": {"template": {"metadata": {"annotations": patch}}}})
        r = kubectl("patch", *dep, "--type=merge", "-p", body, *tenant)
        assert r.returncode != 0 and POLICY in r.stderr, (patch, r.stderr)


def _runtime_class(name: str) -> bool:
    return kubectl("get", "runtimeclass", name).returncode == 0


@pytest.mark.parametrize(
    ("runtime_class", "annotation"),
    [
        # Both fail sandbox Create if containerd forwards them.
        ("k7", {"k7d.katakate.org/backend": "bogus"}),
        ("k7-fc", {"k7d.katakate.org/backend": "bogus"}),
        ("kata", {"io.katacontainers.config_path": "/nonexistent/k7.toml"}),
        ("kata-qemu", {"io.katacontainers.config_path": "/nonexistent/k7.toml"}),
    ],
)
def test_containerd_does_not_forward_other_annotations(test_namespace, runtime_class, annotation):
    if not _runtime_class(runtime_class):
        pytest.skip(f"RuntimeClass {runtime_class} absent")
    pod = _pod(annotation, runtime_class)
    obj = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "fwd", "namespace": test_namespace, **pod["metadata"]},
    }
    assert _apply({**obj, "spec": pod["spec"]}).returncode == 0
    r = kubectl("-n", test_namespace, "wait", "pod/fwd", "--for=condition=Ready", "--timeout=300s")
    events = kubectl("-n", test_namespace, "get", "events", "--field-selector=involvedObject.name=fwd").stdout
    assert r.returncode == 0, events
