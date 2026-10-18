"""Only k7 may set the pod annotations containerd forwards to the k7d shim and
Kata (admission policy k7-runtime-annotations), and containerd forwards only
the ones k7 sets."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import celpy
import pytest
import yaml
from celpy import adapter

from tests.unit.conftest import DEPLOY_DIR
from tests.unit.test_helm_chart import _sets, find, render
from tests.unit.test_playbook_distro_adapter import COMBOS, _render

POLICY = DEPLOY_DIR / "manifests/k7-runtime-annotations/admission-policy.yaml"
CORE = Path(__file__).resolve().parents[2] / "src/k7/core"
K7_API = "system:serviceaccount:kube-system:k7-api"
TENANT = "system:serviceaccount:tenant-a:dev"
CLUSTER_ID = {"k7d.katakate.org/cluster-id": "c1"}
KINDS = ["Pod", "Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job", "ReplicationController", "CronJob"]


def _policy(docs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    docs = docs or [d for d in yaml.safe_load_all(POLICY.read_text()) if d]
    return find(docs, "ValidatingAdmissionPolicy", "k7-runtime-annotations")


def _cel(expr: str, act: dict[str, Any]) -> Any:
    env = celpy.Environment()
    out = env.program(env.compile(expr)).evaluate(adapter.json_to_cel(act))
    if isinstance(out, celpy.CELEvalError):
        raise AssertionError(f"{expr!r}: {out}")
    return out


def admit(policy: dict[str, Any], kind: str, obj: dict, old: dict | None = None, user: str = TENANT, groups=None):
    """True if the policy admits the request (a failed matchCondition skips it)."""
    request = {
        "operation": "UPDATE" if old is not None else "CREATE",
        "kind": {"kind": kind},
        "userInfo": {"username": user, **({"groups": groups} if groups else {})},
    }
    act: dict[str, Any] = {"request": request, "object": obj, "oldObject": old}
    spec = policy["spec"]
    if not all(_cel(m["expression"], act) for m in spec["matchConditions"]):
        return True
    variables: dict[str, Any] = {}
    for v in spec["variables"]:
        variables[v["name"]] = _cel(v["expression"], {**act, "variables": variables})
        act["variables"] = variables
    return all(_cel(v["expression"], act) for v in spec["validations"])


def workload(kind: str, annotations: dict[str, str] | None = None, **extra: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"labels": {"app": "x"}}
    if annotations is not None:
        meta["annotations"] = {**annotations, **extra}
    pod = {"metadata": meta, "spec": {"containers": [{"name": "c", "image": "busybox"}]}}
    if kind == "Pod":
        return pod
    if kind == "CronJob":
        return {"metadata": {"name": "x"}, "spec": {"jobTemplate": {"spec": {"template": pod}}}}
    return {"metadata": {"name": "x"}, "spec": {"template": pod}}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "annotation",
    [
        CLUSTER_ID,
        {"k7d.katakate.org/fork-source-vm": "abc"},
        {"k7d.katakate.org/tree-adopt-cluster": "abc"},
        {"io.katacontainers.config_path": "/opt/kata/share/defaults/kata-containers/configuration.toml"},
        {"io.katacontainers.config.runtime.disable_guest_seccomp": "true"},
    ],
)
def test_tenant_cannot_create_reserved_annotation(kind, annotation):
    policy = _policy()
    assert not admit(policy, kind, workload(kind, annotation))
    assert admit(policy, kind, workload(kind, {"team": "a"}))
    assert admit(policy, kind, workload(kind))


@pytest.mark.parametrize("kind", KINDS)
def test_empty_template_metadata_is_admitted(kind):
    obj = workload(kind)
    pod = obj if kind == "Pod" else (obj["spec"].get("jobTemplate", {}).get("spec") or obj["spec"])["template"]
    del pod["metadata"]
    assert admit(_policy(), kind, obj)


@pytest.mark.parametrize(
    ("user", "groups", "allowed"),
    [
        (K7_API, None, True),
        ("system:admin", ["system:masters", "system:authenticated"], True),
        ("system:kube-controller-manager", None, True),
        ("system:serviceaccount:kube-system:replicaset-controller", None, True),
        ("system:serviceaccount:kube-system:deployment-controller", None, True),
        ("system:serviceaccount:kube-system:default", None, False),
        ("system:serviceaccount:k7-system:k7-api", None, False),
        ("alice", ["tenant-a-admins", "system:authenticated"], False),
        (TENANT, None, False),
    ],
)
def test_only_k7_admins_and_controllers_are_exempt(user, groups, allowed):
    obj = workload("Deployment", CLUSTER_ID)
    assert admit(_policy(), "Deployment", obj, user=user, groups=groups) is allowed


def test_tenant_update_may_keep_but_not_change_reserved_annotations():
    policy = _policy()
    k7 = {"k7d.katakate.org/docker": "true", "io.katacontainers.config.hypervisor.default_memory": "2048"}
    old = workload("Deployment", k7)
    assert admit(policy, "Deployment", workload("Deployment", k7, **{"kubectl.kubernetes.io/restartedAt": "now"}), old)
    assert not admit(policy, "Deployment", workload("Deployment", {**k7, **CLUSTER_ID}), old)
    assert not admit(policy, "Deployment", workload("Deployment", {**k7, "k7d.katakate.org/docker": "false"}), old)
    assert not admit(policy, "Deployment", workload("Deployment", {"k7d.katakate.org/docker": "true"}), old)
    assert admit(policy, "Deployment", workload("Deployment", {**k7, **CLUSTER_ID}), old, user=K7_API)


def test_policy_shape_and_binding():
    docs = [d for d in yaml.safe_load_all(POLICY.read_text()) if d]
    spec = _policy(docs)["spec"]
    assert spec["failurePolicy"] == "Fail"
    rules = {
        (g, r)
        for rule in spec["matchConstraints"]["resourceRules"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
    }
    assert all(rule["operations"] == ["CREATE", "UPDATE"] for rule in spec["matchConstraints"]["resourceRules"])
    assert rules == {
        ("", "pods"),
        ("", "replicationcontrollers"),
        *(("apps", r) for r in ("deployments", "replicasets", "statefulsets", "daemonsets")),
        ("batch", "jobs"),
        ("batch", "cronjobs"),
    }
    binding = find(docs, "ValidatingAdmissionPolicyBinding", "k7-runtime-annotations")
    assert binding["spec"] == {"policyName": "k7-runtime-annotations", "validationActions": ["Deny"]}


@pytest.mark.parametrize("namespace", ["kube-system", "k7-system"])
def test_chart_policy_matches_playbook(namespace):
    docs = render(_sets(), namespace=namespace)
    want = yaml.safe_load_all(POLICY.read_text().replace("kube-system:k7-api", f"{namespace}:k7-api"))
    for doc in (d for d in want if d):
        assert find(docs, doc["kind"], doc["metadata"]["name"])["spec"] == doc["spec"]
    chart = _policy(docs)
    assert admit(
        chart, "Deployment", workload("Deployment", CLUSTER_ID), user=f"system:serviceaccount:{namespace}:k7-api"
    )


def test_playbook_applies_policy_without_the_api_gate():
    tasks = yaml.safe_load((DEPLOY_DIR / "roles/k7_cluster/tasks/nodes.yml").read_text())
    (task,) = [t for t in tasks if "k7-runtime-annotations/admission-policy.yaml" in str(t)]
    assert "when" not in task and task["run_once"] is True


def _forwarded(runtime: str) -> set[str]:
    toml = _render("drop-in", (1, 1, 1, 1))
    block = toml.split(f"containerd.runtimes.{runtime}]\n", 1)[1].split("\n[", 1)[0]
    (line,) = re.findall(r"pod_annotations = \[(.*)\]", block)
    return {s.strip(' "') for s in line.split(",")}


def test_containerd_forwards_exactly_what_core_sets():
    literals = set()
    for f in CORE.glob("*.py"):
        literals |= set(re.findall(r'"((?:k7d\.katakate\.org/|io\.katacontainers\.)[a-z0-9_./-]+)"', f.read_text()))
    k7d = {a for a in literals if a.startswith("k7d.")}
    kata = literals - k7d
    assert k7d == {
        "k7d.katakate.org/docker",
        "k7d.katakate.org/docker-disk",
        "k7d.katakate.org/fork-source-cluster",
        "k7d.katakate.org/fork-source-vm",
    }
    assert kata == {
        f"io.katacontainers.config.hypervisor.{k}"
        for k in ("default_memory", "rx_rate_limiter_max_rate", "tx_rate_limiter_max_rate")
    }
    assert _forwarded("k7") == _forwarded("k7-fc") == k7d
    assert _forwarded("kata") == _forwarded("kata-qemu") == kata
    for combo in COMBOS:
        assert "*" not in "".join(re.findall(r"pod_annotations = .*", _render("k3s-template", combo)))


def test_kata_enables_only_the_memory_annotation():
    tasks = yaml.safe_load((DEPLOY_DIR / "roles/k7_node_prep/tasks/runtimes.yml").read_text())
    lines = [t["ansible.builtin.lineinfile"]["line"] for t in tasks if "enable_annotations" in str(t)]
    assert (
        lines == ['enable_annotations = ["default_memory", "rx_rate_limiter_max_rate", "tx_rate_limiter_max_rate"]'] * 2
    )
