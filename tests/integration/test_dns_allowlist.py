"""FQDN-allowlist sandboxes resolve only allowlisted names (DNS exfiltration fix).

Cilium's DNS proxy forwards a query only when the sandbox's ``{name}-egress``
DNS rule allows the name, and answers every other query itself with NXDOMAIN.
Per backend and libc (musl: Alpine, glibc: Debian), with real packets:

- the allowlisted name resolves through the ``ndots:5`` search list (each
  ``<name>.<ns>.svc.cluster.local`` candidate is refused first) and connects;
- a name that exists upstream but is not allowlisted gets NXDOMAIN, which only
  the proxy can produce (upstream would answer NOERROR);
- no refused query reaches CoreDNS at all: its TXT request counter does not
  move for refused TXT queries, and does for an allowlisted one.

Also: the k7-api startup reconcile rewrites a pre-fix policy (DNS rule
``matchPattern: "*"``) on a live sandbox, idempotently.
"""

import asyncio
import base64
import json
import re
import subprocess
import urllib.request
import uuid

import pytest

from k7.core.core import K7Core, _cnp_dns_matchers, _fqdn_matchers
from k7.core.models import SandboxConfig

from .cluster import KUBECTL
from .test_cilium import _wait_ready, requires_cilium
from .test_egress_matrix import _BACKENDS, _LOCAL_NODE, _node_has_backend
from .test_sandbox_ingress import _read_cnp

pytestmark = [pytest.mark.integration, requires_cilium]

_IMAGES = {"musl": "python:3.12-alpine3.20", "glibc": "python:3.12-slim-bookworm"}
_ALLOWED = "example.com"
# Exists upstream: NOERROR from CoreDNS, so NXDOMAIN can only come from the proxy.
_DENIED = "www.iana.org"
_A, _TXT = 1, 16
_NOERROR, _NXDOMAIN = 0, 3

_PROBE = r"""
import json, random, socket, struct, sys

def rcode(name, qtype):
    ns = next(l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver"))
    q = struct.pack(">HHHHHH", random.randrange(65536), 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"
    q += struct.pack(">HH", qtype, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    for _ in range(3):
        try:
            s.sendto(q, (ns, 53))
            return s.recv(4096)[3] & 15
        except socket.timeout:
            pass
    return -1

def resolves(name):
    try:
        return bool(socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM))
    except socket.gaierror:
        return False

def connects(name):
    try:
        socket.create_connection((name, 443), 10).close()
        return True
    except OSError:
        return False

op, name = sys.argv[1], sys.argv[2]
out = rcode(name, int(sys.argv[3])) if op == "rcode" else {"resolves": resolves, "connects": connects}[op](name)
print(json.dumps(out))
"""
_PROBE_B64 = base64.b64encode(_PROBE.encode()).decode()


async def _probe(k7_core: K7Core, sandbox: str, namespace: str, *args: object):
    cmd = f"echo {_PROBE_B64} | base64 -d > /tmp/dnsprobe.py && python3 /tmp/dnsprobe.py {' '.join(map(str, args))}"
    r = await k7_core.exec_command(sandbox, cmd, namespace=namespace)
    assert r.exit_code == 0, f"probe {args} in {sandbox} failed: {r!r}"
    return json.loads(r.stdout.strip().splitlines()[-1])


async def _eventually(k7_core: K7Core, sandbox: str, namespace: str, want, *args: object, tries: int = 10):
    got = None
    for _ in range(tries):
        got = await _probe(k7_core, sandbox, namespace, *args)
        if got == want:
            return
        await asyncio.sleep(3)
    raise AssertionError(f"{sandbox}: probe {args} = {got!r}, want {want!r}")


def _coredns_txt_requests() -> float:
    """Sum of CoreDNS ``coredns_dns_requests_total{type="TXT"}`` over every CoreDNS pod."""
    out = subprocess.run(
        [
            *KUBECTL,
            "-n",
            "kube-system",
            "get",
            "pods",
            "-l",
            "k8s-app=kube-dns",
            "-o",
            "jsonpath={.items[*].status.podIP}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    ips = out.stdout.split()
    assert ips, "no CoreDNS pods (kube-system, k8s-app=kube-dns)"
    total = 0.0
    for ip in ips:
        with urllib.request.urlopen(f"http://{ip}:9153/metrics", timeout=10) as resp:
            text = resp.read().decode()
        for m in re.finditer(r"^coredns_dns_requests_total\{([^}]*)\} (\S+)$", text, re.M):
            if 'type="TXT"' in m.group(1):
                total += float(m.group(2))
    return total


def _short(backend: str) -> str:
    return {"kata-firecracker-devmapper": "kfd", "kata-qemu-longhorn": "kql"}.get(backend, backend)


@pytest.mark.parametrize("libc", list(_IMAGES))
@pytest.mark.parametrize("backend", _BACKENDS)
async def test_fqdn_sandbox_resolves_only_allowlisted_names(
    k7_core: K7Core, test_namespace: str, backend: str, libc: str
):
    if not _node_has_backend(backend):
        pytest.skip(f"node {_LOCAL_NODE} lacks label k7.katakate.org/backend-{backend}=true")
    name = f"dns-{_short(backend)}-{libc}"
    cfg = SandboxConfig(
        name=name,
        image=_IMAGES[libc],
        namespace=test_namespace,
        backend=backend,
        egress_whitelist=[_ALLOWED],
        node_name=_LOCAL_NODE,
    )
    try:
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create {name} failed: {result.error}"
        await _wait_ready(k7_core, name, test_namespace, timeout=600)
        cnp = await _read_cnp(k7_core, f"{name}-egress", test_namespace)
        assert _cnp_dns_matchers(cnp) == _fqdn_matchers([_ALLOWED]), cnp["spec"]

        await _eventually(k7_core, name, test_namespace, True, "resolves", _ALLOWED)
        await _eventually(k7_core, name, test_namespace, True, "connects", _ALLOWED)
        assert await _probe(k7_core, name, test_namespace, "rcode", _ALLOWED, _A) == _NOERROR

        assert await _probe(k7_core, name, test_namespace, "resolves", _DENIED) is False
        assert await _probe(k7_core, name, test_namespace, "rcode", _DENIED, _A) == _NXDOMAIN
        exfil = f"x{uuid.uuid4().hex}.example.net"
        assert await _probe(k7_core, name, test_namespace, "rcode", exfil, _A) == _NXDOMAIN

        before = _coredns_txt_requests()
        for _ in range(5):
            assert await _probe(k7_core, name, test_namespace, "rcode", f"x{uuid.uuid4().hex}.example.net", _TXT) == (
                _NXDOMAIN
            )
        assert _coredns_txt_requests() == before, "a refused query reached CoreDNS"
        assert await _probe(k7_core, name, test_namespace, "rcode", _ALLOWED, _TXT) == _NOERROR
        assert _coredns_txt_requests() > before, "control: the allowlisted TXT query must reach CoreDNS"
    finally:
        r = await k7_core.delete_sandbox(name, namespace=test_namespace)
        assert r.success, f"delete {name} failed: {r.error}"


async def test_reconcile_rewrites_a_pre_fix_policy(k7_core: K7Core, test_namespace: str):
    backend = next((b for b in _BACKENDS if _node_has_backend(b)), None)
    if backend is None:
        pytest.skip(f"node {_LOCAL_NODE} has no k7 backend label")
    name, ref = "dns-reconcile", f"{test_namespace}/dns-reconcile-egress"
    cfg = SandboxConfig(
        name=name,
        image=_IMAGES["musl"],
        namespace=test_namespace,
        backend=backend,
        egress_whitelist=[_ALLOWED],
        node_name=_LOCAL_NODE,
    )
    custom = await k7_core._get_custom_objects_client()
    try:
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create {name} failed: {result.error}"
        await _wait_ready(k7_core, name, test_namespace, timeout=600)

        cnp = await _read_cnp(k7_core, f"{name}-egress", test_namespace)
        cnp["spec"]["egress"][0]["toPorts"][0]["rules"]["dns"] = [{"matchPattern": "*"}]
        await custom.replace_namespaced_custom_object(
            "cilium.io", "v2", test_namespace, "ciliumnetworkpolicies", f"{name}-egress", cnp
        )
        await _eventually(k7_core, name, test_namespace, _NOERROR, "rcode", _DENIED, _A)

        assert ref in await k7_core.reconcile_cilium_egress_policies()
        assert _cnp_dns_matchers(await _read_cnp(k7_core, f"{name}-egress", test_namespace)) == _fqdn_matchers(
            [_ALLOWED]
        )
        await _eventually(k7_core, name, test_namespace, _NXDOMAIN, "rcode", _DENIED, _A)
        await _eventually(k7_core, name, test_namespace, True, "connects", _ALLOWED)
        assert ref not in await k7_core.reconcile_cilium_egress_policies()
    finally:
        r = await k7_core.delete_sandbox(name, namespace=test_namespace)
        assert r.success, f"delete {name} failed: {r.error}"
