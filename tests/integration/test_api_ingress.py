"""Integration tests for the opt-in k7-api source-CIDR allowlist.

Read this before changing anything here: **the suite runs on the node**, so
its own traffic originates from the `host` entity, which the policy allows
unconditionally (it has to — that is the kubelet probe path). A test that
only curls the NodePort from the node passes no matter how broken the CIDR
list is. So the deny path is exercised from a **pod**, and the pod's
observed source IP is asserted first, so a pass means "the policy decided"
rather than "SNAT hid the client".

The pod is a plain `alpine` pod with no `katakate.org/sandbox` label: a
sandbox cannot reach `k7-api` at all, so a sandbox client would
be blocked by the wrong policy and prove nothing.

Two measured Cilium behaviours shape these tests, both verified on this
cluster:

- A `fromCIDR` peer is not evaluated for traffic whose source is
  Cilium-managed. Allowlisting a pod's own /32 therefore does **not** let
  it in — `test_pod_cidr_in_the_allowlist_is_still_denied` pins that down
  so nobody mistakes the allowlist for an in-cluster access control.
- Node-local traffic to a NodePort is SNAT'd to the pod network even with
  `externalTrafficPolicy: Local`, so the node can neither observe a real
  client IP nor produce a `cidr:` drop. The genuinely-external half of the
  matrix (a real client allowed by its CIDR, a real client denied by it)
  can only be driven from an off-cluster host and is not expressible here.

Rollback, if a run dies between apply and teardown::

    kubectl -n <system namespace> delete ciliumnetworkpolicy k7-api-ingress
    kubectl -n <system namespace> patch svc k7-api \
        -p '{"spec":{"externalTrafficPolicy":"Cluster"}}'
"""

import json
import subprocess
import time
import uuid

import pytest

from .cluster import KUBECTL, system_ns
from .test_api import _api_scheme, _tls_verify
from .test_cilium import requires_cilium

pytestmark = pytest.mark.integration

_POLICY = "k7-api-ingress"
_PROBE_POD = "k7-api-ingress-probe"
_PROBE_NS = "default"

# TEST-NET-2 (RFC 5737). No real client can come from here, so a policy
# carrying only this CIDR allows exactly the reserved entities.
_EXCLUDES_EVERYONE = "198.51.100.0/24"


def _kubectl(*args: str, check: bool = True, stdin: str | None = None) -> str:
    result = subprocess.run(
        [*KUBECTL, *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout


def _policy_body(cidrs: list[str]) -> str:
    """The same shape the install playbook renders from --api-allow-cidr.

    The `fromEntities` block is mandatory: an `ingress:` section flips the
    k7-api endpoint to default-deny, and `host` is the kubelet probe path.
    """
    return json.dumps(
        {
            "apiVersion": "cilium.io/v2",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": _POLICY, "namespace": system_ns()},
            "spec": {
                "endpointSelector": {"matchLabels": {"app": "k7-api"}},
                "ingress": [
                    {"fromEntities": ["host", "remote-node", "health", "kube-apiserver"]},
                    {"fromCIDR": cidrs},
                ],
            },
        }
    )


def _api_pod() -> dict:
    pods = json.loads(_kubectl("-n", system_ns(), "get", "pod", "-l", "app=k7-api", "-o", "json"))["items"]
    live = sorted((p for p in pods if not p["metadata"].get("deletionTimestamp")), key=lambda p: p["metadata"]["name"])
    assert live, "no live k7-api pod"
    # Every replica carries the same endpointSelector policy; probe a stable one.
    return live[0]


def _pod_ready(pod: dict) -> bool:
    conditions = pod["status"].get("conditions") or []
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def _restart_count(pod: dict) -> int:
    return sum(c.get("restartCount", 0) for c in pod["status"].get("containerStatuses") or [])


def _nodeport() -> str:
    return _kubectl("-n", system_ns(), "get", "svc", "k7-api", "-o", "jsonpath={.spec.ports[0].nodePort}").strip()


def _nodeport_health_curl() -> list[str]:
    args = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "6"]
    verify = _tls_verify()
    if isinstance(verify, str):
        args += ["--cacert", verify]
    args.append(f"{_api_scheme()}://127.0.0.1:{_nodeport()}/health")
    return args


def _external_traffic_policy() -> str:
    return _kubectl("-n", system_ns(), "get", "svc", "k7-api", "-o", "jsonpath={.spec.externalTrafficPolicy}").strip()


def _set_external_traffic_policy(value: str) -> None:
    _kubectl(
        "-n",
        system_ns(),
        "patch",
        "svc",
        "k7-api",
        "-p",
        json.dumps({"spec": {"externalTrafficPolicy": value}}),
    )


def _probe_from_pod(url: str, request_id: str | None = None) -> str:
    """ "OK" / "BLOCKED" for an HTTP GET from the probe pod.

    A policy drop is a hang, not a refusal, hence the short timeout and the
    sentinel: "connection refused" would mean the packet got through and the
    listener is the problem.
    """
    header = f"--header 'X-Request-ID: {request_id}' " if request_id else ""
    out = _kubectl(
        "-n",
        _PROBE_NS,
        "exec",
        _PROBE_POD,
        "--",
        "sh",
        "-c",
        f"wget -q -T 6 {header}-O /dev/null '{url}' && echo OK || echo BLOCKED",
        check=False,
    )
    lines = out.strip().splitlines()
    return lines[-1] if lines else "NO-OUTPUT"


def _observed_source_ip(token: str) -> str | None:
    """The ``client_ip`` of k7-api's JSON access line for the request whose
    ``X-Request-ID`` is ``token`` (see ``_probe_api``)."""
    logs = _kubectl("-n", system_ns(), "logs", f"pod/{_api_pod()['metadata']['name']}", "-c", "k7-api", "--tail=500")
    for line in logs.splitlines():
        entry = json.loads(line)
        if entry["request_id"] == token and entry["logger"] == "k7.api":
            return entry["client_ip"]
    return None


def _probe_api(token: str) -> str:
    """GET k7-api's ``/`` (logged at INFO, unlike ``/health``) with ``token`` as its request id."""
    return _probe_from_pod(f"http://{_api_pod()['status']['podIP']}:8000/", request_id=token)


@pytest.fixture(scope="module")
def probe_pod() -> str:
    """A plain pod (no sandbox label) with its own IP, and no policy of its own."""
    manifest = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": _PROBE_POD, "namespace": _PROBE_NS, "labels": {"app": _PROBE_POD}},
            "spec": {
                "containers": [
                    {
                        "name": "probe",
                        "image": "alpine:3.20",
                        "command": ["sleep", "1800"],
                    }
                ],
                "terminationGracePeriodSeconds": 1,
            },
        }
    )
    _kubectl("apply", "-f", "-", stdin=manifest)
    try:
        _kubectl("-n", _PROBE_NS, "wait", f"pod/{_PROBE_POD}", "--for=condition=Ready", "--timeout=120s")
        pod_ip = _kubectl("-n", _PROBE_NS, "get", "pod", _PROBE_POD, "-o", "jsonpath={.status.podIP}").strip()
        assert pod_ip, "probe pod has no IP"
        yield pod_ip
    finally:
        _kubectl("-n", _PROBE_NS, "delete", "pod", _PROBE_POD, "--ignore-not-found", "--wait=false", check=False)


@pytest.fixture
def hardened():
    """Apply the allowlist policy + `Local`, then restore the default state.

    Restoration is the documented rollback; it runs even when the
    body of the test explodes.
    """
    previous = _external_traffic_policy() or "Cluster"

    def apply(cidrs: list[str]) -> None:
        _kubectl("apply", "-f", "-", stdin=_policy_body(cidrs))
        _set_external_traffic_policy("Local")

    try:
        yield apply
    finally:
        _kubectl("-n", system_ns(), "delete", "ciliumnetworkpolicy", _POLICY, "--ignore-not-found", check=False)
        _set_external_traffic_policy(previous)


class TestDefaultInstall:
    def test_no_policy_and_traffic_policy_is_cluster(self):
        """A default install (no --api-allow-cidr) is today's install, bit for bit."""
        out = _kubectl(
            "-n", system_ns(), "get", "ciliumnetworkpolicy", _POLICY, "--ignore-not-found", check=False
        ).strip()
        assert out == "", f"{_POLICY} exists on a cluster installed without --api-allow-cidr: {out}"
        assert _external_traffic_policy() == "Cluster"

    def test_api_answers_the_nodeport(self):
        code = subprocess.run(
            _nodeport_health_curl(),
            capture_output=True,
            text=True,
        ).stdout
        assert code == "200", f"k7-api /health returned {code!r} before any policy was applied"


@requires_cilium
class TestAllowlistPolicy:
    def test_pod_to_api_source_ip_is_not_snatted(self, probe_pod: str):
        """Prerequisite for every deny assertion below.

        If the pod's packets arrived as some other address, a later "BLOCKED"
        would say nothing about the CIDR list.
        """
        token = uuid.uuid4().hex
        verdict = _probe_api(token)
        assert verdict == "OK", f"plain pod cannot reach k7-api with no policy applied: {verdict}"
        assert _observed_source_ip(token) == probe_pod, (
            f"k7-api observed {_observed_source_ip(token)!r}, not the probe pod IP {probe_pod!r} — "
            "the deny tests below would be testing SNAT, not the policy"
        )

    def test_pod_outside_the_allowlist_is_denied(self, probe_pod: str, hardened):
        hardened([_EXCLUDES_EVERYONE])
        # The policy takes a moment to reach the endpoint's BPF maps.
        deadline = time.time() + 30
        while time.time() < deadline:
            token = uuid.uuid4().hex
            if _probe_api(token) == "BLOCKED":
                break
            time.sleep(3)
        else:
            pytest.fail("pod outside the allowlist still reaches k7-api after 30s of policy")
        assert _observed_source_ip(token) is None, "a request that should have been dropped reached k7-api"

    def test_pod_cidr_in_the_allowlist_is_still_denied(self, probe_pod: str, hardened):
        """Measured Cilium behaviour, asserted so it cannot be mistaken for a bug.

        `fromCIDR` is not evaluated when the source is Cilium-managed, so the
        allowlist is a control on *external* clients only. In-cluster callers
        need `fromEndpoints` (see `agent-networkpolicy.yaml`).
        """
        hardened([f"{probe_pod}/32"])
        url = f"http://{_api_pod()['status']['podIP']}:8000/health"
        deadline = time.time() + 30
        while time.time() < deadline:
            if _probe_from_pod(url) == "BLOCKED":
                return
            time.sleep(3)
        pytest.fail(f"{probe_pod}/32 in fromCIDR let a Cilium-managed pod through — behaviour changed, update the docs")

    def test_api_pod_stays_ready_under_policy(self, hardened):
        """The regression this feature is most likely to cause: dropping the
        `host` entity kills the kubelet probes and crash-loops the pod."""
        before = _api_pod()
        hardened([_EXCLUDES_EVERYONE])
        restarts = _restart_count(before)
        deadline = time.time() + 65
        while time.time() < deadline:
            pod = _api_pod()
            assert _pod_ready(pod), f"k7-api pod went NotReady under the ingress policy: {pod['status']}"
            assert _restart_count(pod) == restarts, "k7-api container restarted under the ingress policy"
            time.sleep(5)

    def test_node_still_reaches_the_nodeport_under_policy(self, hardened):
        """The `host` entity path — deliberately allowed, and deliberately
        useless as a deny test: this passes however wrong the CIDR list is."""
        hardened([_EXCLUDES_EVERYONE])
        code = subprocess.run(
            _nodeport_health_curl(),
            capture_output=True,
            text=True,
        ).stdout
        assert code == "200", f"node-local /health returned {code!r} under the ingress policy"

    def test_rollback_restores_the_default_state(self, probe_pod: str, hardened):
        hardened([_EXCLUDES_EVERYONE])
        assert _external_traffic_policy() == "Local"
        _kubectl("-n", system_ns(), "delete", "ciliumnetworkpolicy", _POLICY)
        _set_external_traffic_policy("Cluster")
        url = f"http://{_api_pod()['status']['podIP']}:8000/health"
        deadline = time.time() + 30
        while time.time() < deadline:
            if _probe_from_pod(url) == "OK":
                return
            time.sleep(3)
        pytest.fail("rollback did not restore pod access to k7-api")
