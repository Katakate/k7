"""Integration tests for sandbox network policy.

M0 covers the fork regression: ``k7 fork`` used to produce a sandbox with no
NetworkPolicy and no CiliumNetworkPolicy at all, i.e. wide-open egress and
ingress no matter how locked down its source was.
"""

import asyncio
import ipaddress
import subprocess
import time

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from k7.core.core import K7Core
from k7.core.models import OperationResult, SandboxConfig

pytestmark = pytest.mark.integration

_K3S = "/usr/local/bin/k3s"


def _cilium_crd_present() -> bool:
    try:
        result = subprocess.run(
            [_K3S, "kubectl", "get", "crd", "ciliumnetworkpolicies.cilium.io"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


requires_cilium = pytest.mark.skipif(
    not _cilium_crd_present(),
    reason="Cilium CRD not installed (cluster likely running Flannel)",
)


async def _wait_ready(k7_core: K7Core, name: str, namespace: str, timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        sandboxes = await k7_core.list_sandboxes(namespace=namespace)
        if any(s.name == name and s.ready == "True" for s in sandboxes):
            return
        await asyncio.sleep(3)
    raise AssertionError(f"Sandbox {name} never became Ready within {timeout}s")


async def _read_netpol(k7_core: K7Core, name: str, namespace: str):
    net = await k7_core._get_networking_v1_client()
    return await net.read_namespaced_network_policy(name=name, namespace=namespace)


async def _read_cnp(k7_core: K7Core, name: str, namespace: str):
    custom = await k7_core._get_custom_objects_client()
    return await custom.get_namespaced_custom_object(
        group="cilium.io",
        version="v2",
        namespace=namespace,
        plural="ciliumnetworkpolicies",
        name=name,
    )


def _deployment_exists(name: str, namespace: str) -> bool:
    result = subprocess.run(
        [_K3S, "kubectl", "-n", namespace, "get", "deployment", name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


async def _serve(k7_core: K7Core, name: str, namespace: str, port: int) -> None:
    """Start a persistent HTTP listener inside a sandbox, serving `/` and a
    `/cgi-bin/ip` that reports the client IP the sandbox actually saw.

    busybox-extras `httpd` rather than `nc -l` or socat, both of which were tried
    and produced false drops: `printf … | nc -l` never holds a listening socket
    (nc exits when its stdin pipe hits EOF), and `socat …,fork SYSTEM:` drops
    roughly one connection in ten under load. Either way a real allow rule reads
    as a policy drop, which is the one mistake these tests must not make. httpd
    is a real daemon and measured 20/20 on the same probe loop that flapped
    under socat. `httpd` is not in alpine's stock busybox applet set.
    """
    root = f"/srv{port}"
    setup = (
        "apk add --no-cache busybox-extras >/dev/null 2>&1; "
        f"mkdir -p {root}/cgi-bin; echo hi > {root}/index.html; "
        f'printf "#!/bin/sh\\necho Content-Type: text/plain\\necho\\necho \\$REMOTE_ADDR\\n" > {root}/cgi-bin/ip; '
        f"chmod +x {root}/cgi-bin/ip; "
        f"httpd -p {port} -h {root}; "
        f"for _ in 1 2 3 4 5; do netstat -ltn | grep -q ':{port} ' && break; sleep 1; done; "
        f"netstat -ltn | grep ':{port} '"
    )
    result = await k7_core.exec_command(name, setup, namespace=namespace)
    assert f":{port}" in result.stdout, f"listener on {port} never bound: {result.stdout!r} {result.stderr!r}"


async def _probe(k7_core: K7Core, client_sb: str, target_ip: str, port: int, namespace: str) -> str:
    """Return "OK" or "BLOCKED" for an HTTP GET from ``client_sb`` — the
    `&& echo OK || echo BLOCKED` sentinel style `test_cilium.py` uses, because a
    policy drop shows up as a hang, not a refusal.

    That distinction is the debugging tool when one of these tests fails:
    "connection refused" in `wget`'s output means the policy passed the packet
    and the listener is the problem, a timeout means the policy dropped it.
    """
    result = await k7_core.exec_command(
        client_sb,
        f"wget -q -T 6 -O /dev/null http://{target_ip}:{port}/ && echo OK || echo BLOCKED",
        namespace=namespace,
    )
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else f"NO-OUTPUT({result.stderr!r})"


async def _pod_ip(k7_core: K7Core, name: str, namespace: str) -> str:
    v1 = await k7_core._get_core_v1_client()
    pods = await v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={name}")
    live = [p for p in pods.items if not p.metadata.deletion_timestamp]
    assert live and live[0].status.pod_ip, f"no pod IP for {name}"
    return live[0].status.pod_ip


class TestForkInheritsNetworkPolicy:
    """Default-deny regression tests."""

    @requires_cilium
    async def test_fork_of_fqdn_egress_sandbox_inherits_policies(self, k7_core: K7Core, test_namespace: str):
        source, fork = "integ-np-src", "integ-np-fork"
        cfg = SandboxConfig(
            name=source,
            image="alpine:3.20",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            egress_whitelist=["example.com"],
        )
        # No `before_script`: a fork inherits the source's readiness probe
        # (`test -f /tmp/k7_before_done_<source>`), and /tmp is not on the
        # cloned disk, so such a fork never goes Ready. Use alpine's built-in
        # busybox wget instead of installing curl.
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            await _wait_ready(k7_core, source, test_namespace)

            fork_result = await k7_core.fork_sandbox(source, fork, namespace=test_namespace)
            assert fork_result.success, f"fork failed: {fork_result.error}"

            cnp = await _read_cnp(k7_core, f"{fork}-egress", test_namespace)
            egress = cnp["spec"]["egress"]
            assert any(
                "toFQDNs" in r and any(m.get("matchName") == "example.com" for m in r["toFQDNs"]) for r in egress
            ), f"fork did not inherit the source's FQDN egress: {egress!r}"
            assert cnp["spec"]["endpointSelector"]["matchLabels"]["katakate.org/sandbox"] == fork

            deny = await _read_netpol(k7_core, f"{fork}-deny-ingress", test_namespace)
            assert deny.spec.policy_types == ["Ingress"]
            assert deny.spec.ingress in (None, [])

            await _wait_ready(k7_core, fork, test_namespace)

            allowed = await k7_core.exec_command(
                fork,
                "wget -q -T 20 -O /dev/null https://example.com/ && echo OK || echo BLOCKED",
                namespace=test_namespace,
            )
            assert "OK" in allowed.stdout, (
                f"whitelisted domain unreachable from fork: stdout={allowed.stdout!r} stderr={allowed.stderr!r}"
            )

            blocked = await k7_core.exec_command(
                fork,
                "wget -q -T 8 -O /dev/null https://www.iana.org/ && echo OK || echo BLOCKED",
                namespace=test_namespace,
            )
            assert "BLOCKED" in blocked.stdout, (
                f"fork reached a non-whitelisted domain — it inherited no egress policy "
                f"stdout={blocked.stdout!r} stderr={blocked.stderr!r}"
            )
        finally:
            await k7_core.delete_sandbox(fork, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)

    async def test_fork_of_open_egress_sandbox_gets_no_egress_policy(self, k7_core: K7Core, test_namespace: str):
        """Correct inheritance, not a bug: an open-egress source forks open."""
        source, fork = "integ-np-open-src", "integ-np-open-fork"
        cfg = SandboxConfig(
            name=source,
            image="alpine:3.20",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            egress_whitelist=None,
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            await _wait_ready(k7_core, source, test_namespace)

            fork_result = await k7_core.fork_sandbox(source, fork, namespace=test_namespace)
            assert fork_result.success, f"fork failed: {fork_result.error}"

            with pytest.raises(ApiException) as excinfo:
                await _read_netpol(k7_core, f"{fork}-netpol", test_namespace)
            assert excinfo.value.status == 404

            # Ingress is still denied — that default is not inherited, it is absolute.
            deny = await _read_netpol(k7_core, f"{fork}-deny-ingress", test_namespace)
            assert deny.spec.ingress in (None, [])
        finally:
            await k7_core.delete_sandbox(fork, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)

    async def test_fork_policy_failure_rolls_back_deployment(
        self, k7_core: K7Core, test_namespace: str, monkeypatch: pytest.MonkeyPatch
    ):
        """A fork whose policy creation fails must not survive: an unrestricted
        sandbox is exactly the failure mode M0 fixes."""
        source, fork = "integ-np-rb-src", "integ-np-rb-fork"
        cfg = SandboxConfig(
            name=source,
            image="alpine:3.20",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            egress_whitelist=["10.0.0.0/8"],
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            await _wait_ready(k7_core, source, test_namespace)

            real_apply = k7_core._apply_sandbox_network_policies

            async def _fail_for_fork(name: str, namespace: str, *args, **kwargs):
                if name == fork:
                    return OperationResult(success=False, error="injected policy failure")
                return await real_apply(name, namespace, *args, **kwargs)

            monkeypatch.setattr(k7_core, "_apply_sandbox_network_policies", _fail_for_fork)

            fork_result = await k7_core.fork_sandbox(source, fork, namespace=test_namespace)
            assert not fork_result.success
            assert "injected policy failure" in fork_result.error
            assert not _deployment_exists(fork, test_namespace), (
                "fork Deployment survived a policy failure — it would be running unrestricted"
            )
        finally:
            await k7_core.delete_sandbox(fork, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)


class TestSandboxIngress:
    """opt-in in-cluster ingress, deny-all default."""

    async def _sandbox(self, k7_core: K7Core, namespace: str, name: str, **kwargs) -> None:
        cfg = SandboxConfig(name=name, image="alpine:3.20", namespace=namespace, **kwargs)
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create {name} failed: {result.error}"
        await _wait_ready(k7_core, name, namespace, timeout=300)

    async def test_default_denies_all_ingress(self, k7_core: K7Core, test_namespace: str):
        """The default policy is byte-identical to the one k7 always created,
        and it actually blocks a same-namespace peer."""
        server, client_sb = "integ-ing-deny", "integ-ing-deny-peer"
        try:
            await self._sandbox(k7_core, test_namespace, server)
            await self._sandbox(k7_core, test_namespace, client_sb)

            deny = await _read_netpol(k7_core, f"{server}-deny-ingress", test_namespace)
            assert deny.spec.policy_types == ["Ingress"]
            assert deny.spec.ingress in (None, [])

            await _serve(k7_core, server, test_namespace, 8000)
            ip = await _pod_ip(k7_core, server, test_namespace)
            assert await _probe(k7_core, client_sb, ip, 8000, test_namespace) == "BLOCKED"
        finally:
            await k7_core.delete_sandbox(client_sb, namespace=test_namespace)
            await k7_core.delete_sandbox(server, namespace=test_namespace)

    async def test_ingress_port_opens_only_that_port(self, k7_core: K7Core, test_namespace: str):
        server, client_sb = "integ-ing-open", "integ-ing-open-peer"
        try:
            await self._sandbox(k7_core, test_namespace, server, ingress_ports=[8000])
            await self._sandbox(k7_core, test_namespace, client_sb)

            await _serve(k7_core, server, test_namespace, 8000)
            await _serve(k7_core, server, test_namespace, 8001)
            ip = await _pod_ip(k7_core, server, test_namespace)

            assert await _probe(k7_core, client_sb, ip, 8000, test_namespace) == "OK"
            assert await _probe(k7_core, client_sb, ip, 8001, test_namespace) == "BLOCKED"
        finally:
            await k7_core.delete_sandbox(client_sb, namespace=test_namespace)
            await k7_core.delete_sandbox(server, namespace=test_namespace)

    async def test_ingress_from_scopes_to_one_sandbox(self, k7_core: K7Core, test_namespace: str):
        server, allowed, denied = "integ-ing-scope", "integ-ing-a", "integ-ing-c"
        try:
            await self._sandbox(k7_core, test_namespace, allowed)
            await self._sandbox(k7_core, test_namespace, denied)
            await self._sandbox(
                k7_core, test_namespace, server, ingress_ports=[8000], ingress_from=[f"sandbox:{allowed}"]
            )

            await _serve(k7_core, server, test_namespace, 8000)
            ip = await _pod_ip(k7_core, server, test_namespace)

            assert await _probe(k7_core, allowed, ip, 8000, test_namespace) == "OK"
            assert await _probe(k7_core, denied, ip, 8000, test_namespace) == "BLOCKED"
        finally:
            for name in (denied, allowed, server):
                await k7_core.delete_sandbox(name, namespace=test_namespace)

    @requires_cilium
    async def test_cidr_source_does_not_restrict_in_cluster_peers(self, k7_core: K7Core, test_namespace: str):
        """Pins the `cidr:` caveat that `k7 create` warns about.

        Cilium does not evaluate an `ipBlock` peer for traffic where both ends are
        Cilium-managed, so a rule whose peers are all `cidr:` degrades to a
        port-only allow: an unrelated sandbox reaches the opened port even though
        the CIDR here contains no cluster IP at all. The port list is still
        enforced, so this is a peer-scoping hole, not a blanket allow.

        If this ever starts returning BLOCKED — a Cilium upgrade, or
        `policy-cidr-match-mode=pods` — the warning and the docs are what need
        updating.
        """
        server, peer = "integ-ing-cidr", "integ-ing-cidr-peer"
        try:
            await self._sandbox(k7_core, test_namespace, peer)
            await self._sandbox(
                k7_core,
                test_namespace,
                server,
                ingress_ports=[8000],
                ingress_from=["cidr:198.51.100.0/24"],
            )

            await _serve(k7_core, server, test_namespace, 8000)
            ip = await _pod_ip(k7_core, server, test_namespace)
            assert await _probe(k7_core, peer, ip, 8000, test_namespace) == "OK"
            assert await _probe(k7_core, peer, ip, 9999, test_namespace) == "BLOCKED"
        finally:
            for name in (peer, server):
                await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_unparseable_source_fails_at_create(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-ing-bad",
            image="alpine:3.20",
            namespace=test_namespace,
            ingress_ports=[8000],
            ingress_from=["nonsense"],
        )
        result = await k7_core.create_sandbox(cfg)
        assert not result.success
        assert "nonsense" in result.error
        assert not _deployment_exists("integ-ing-bad", test_namespace)

    async def test_sources_without_ports_fail_at_create(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-ing-noport",
            image="alpine:3.20",
            namespace=test_namespace,
            ingress_from=["sandbox:whoever"],
        )
        result = await k7_core.create_sandbox(cfg)
        assert not result.success
        assert "ingress_from requires ingress_ports" in result.error
        assert not _deployment_exists("integ-ing-noport", test_namespace)

    async def test_fork_inherits_ingress_rules(self, k7_core: K7Core, test_namespace: str):
        source, fork = "integ-ing-fsrc", "integ-ing-ffork"
        try:
            await self._sandbox(
                k7_core,
                test_namespace,
                source,
                backend="kata-qemu-longhorn",
                ingress_ports=[8000],
                ingress_from=["sandbox:integ-ing-peer"],
            )

            fork_result = await k7_core.fork_sandbox(source, fork, namespace=test_namespace)
            assert fork_result.success, f"fork failed: {fork_result.error}"

            np = await _read_netpol(k7_core, f"{fork}-deny-ingress", test_namespace)
            rule = np.spec.ingress[0]
            assert [(p.protocol, p.port) for p in rule.ports] == [("TCP", 8000)]
            assert rule._from[0].pod_selector.match_labels == {"katakate.org/sandbox": "integ-ing-peer"}
            assert np.spec.pod_selector.match_labels == {"katakate.org/sandbox": fork}
        finally:
            await k7_core.delete_sandbox(fork, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)


def _curl(url: str, path: str = "/") -> subprocess.CompletedProcess:
    """Reach a NodePort from the node the suite runs on."""
    return subprocess.run(
        ["curl", "-sS", "--max-time", "8", f"{url}{path}"],
        capture_output=True,
        text=True,
    )


def _curl_until_answered(url: str, attempts: int = 10) -> subprocess.CompletedProcess:
    for _ in range(attempts):
        probe = _curl(url)
        if probe.returncode == 0 and "hi" in probe.stdout:
            return probe
        time.sleep(3)
    raise AssertionError(f"NodePort {url} never answered: {probe.stdout!r} {probe.stderr!r}")


def _observed_source_ip(url: str) -> str:
    """The source IP the sandbox saw for our request, straight out of `httpd`'s
    CGI `REMOTE_ADDR`.

    A NodePort test must establish this before
    asserting anything about a source-CIDR rule, otherwise it cannot tell a
    working policy from SNAT. `httpd` binds dual-stack and reports IPv4 peers
    IPv4-mapped, so unwrap `[::ffff:a.b.c.d]`.
    """
    probe = _curl(url, "/cgi-bin/ip")
    assert probe.returncode == 0, f"could not read back the source IP from {url}: {probe.stderr!r}"
    raw = probe.stdout.strip().strip("[]")
    return raw.rsplit(":", 1)[-1] if raw.lower().startswith("::ffff:") else raw


async def v1_nodes(k7_core: K7Core):
    return await (await k7_core._get_core_v1_client()).list_node()


class TestSandboxExpose:
    """NodePort exposure outside the cluster."""

    async def _exposed(self, k7_core: K7Core, namespace: str, name: str, sources: list[str]) -> str:
        cfg = SandboxConfig(
            name=name,
            image="alpine:3.20",
            namespace=namespace,
            ingress_ports=[8000],
            ingress_from=sources,
            expose_ports=[8000],
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create {name} failed: {result.error}"
        endpoints = (result.data or {}).get("endpoints") or []
        assert len(endpoints) == 1 and endpoints[0]["node_port"], f"no resolved endpoint: {result.data!r}"
        await _wait_ready(k7_core, name, namespace, timeout=300)
        return endpoints[0]["url"]

    async def test_expose_is_reachable_and_cleaned_up(self, k7_core: K7Core, test_namespace: str):
        name = "integ-exp-open"
        try:
            url = await self._exposed(k7_core, test_namespace, name, ["cidr:0.0.0.0/0"])
            await _serve(k7_core, name, test_namespace, 8000)

            v1 = await k7_core._get_core_v1_client()
            svc = await v1.read_namespaced_service(name=f"{name}-expose", namespace=test_namespace)
            # Without `Local` the client's source IP is SNAT'd before the pod
            # sees it and every cidr: rule in front of a NodePort is decoration.
            assert svc.spec.external_traffic_policy == "Local"
            assert svc.spec.type == "NodePort"
            assert [p.node_port for p in svc.spec.ports] == [int(url.rsplit(":", 1)[1])]

            _curl_until_answered(url)

            listed = [s for s in await k7_core.list_sandboxes(namespace=test_namespace) if s.name == name]
            assert listed and listed[0].node_ports == [int(url.rsplit(":", 1)[1])]
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

        remaining = subprocess.run(
            [_K3S, "kubectl", "-n", test_namespace, "get", "svc", "-o", "name"],
            capture_output=True,
            text=True,
        )
        assert remaining.stdout.strip() == "", f"delete leaked Services: {remaining.stdout!r}"

    @requires_cilium
    async def test_node_local_traffic_is_snat_and_not_cidr_scoped(self, k7_core: K7Core, test_namespace: str):
        """Pins why this suite cannot validate `--expose-port`'s source-IP
        behaviour by itself, which is the trap: it runs *on the
        node*, and node-local traffic to a NodePort is both SNAT'd and outside
        what a `cidr:` peer is evaluated against.

        Two things are asserted, and both are caveats rather than features:

        * the pod sees an address on the *pod* network, not the node IP — so
          `externalTrafficPolicy: Local` does not preserve the source for a
          hairpinned request from the node itself;
        * the request is answered even though the allowlist is
          `cidr:198.51.100.0/24`, because Cilium does not evaluate an `ipBlock`
          peer against a node IP without `policy-cidr-match-mode=nodes`.

        So `curl` from the node passes no matter how the rule is written. Both
        halves were verified from a genuinely external client instead, and both
        work there: with `cidr:0.0.0.0/0` the sandbox observed the client's real
        public IP, and with `cidr:198.51.100.0/24` the same client timed out
        while a control sandbox on the same node answered. That check needs an
        off-cluster host, so it stays a manual step — if this test ever starts
        failing, node-local behaviour changed and the docs need revisiting.
        """
        name = "integ-exp-nodelocal"
        try:
            url = await self._exposed(k7_core, test_namespace, name, ["cidr:198.51.100.0/24"])
            await _serve(k7_core, name, test_namespace, 8000)

            _curl_until_answered(url)

            observed = ipaddress.ip_address(_observed_source_ip(url))
            assert observed not in ipaddress.ip_network("198.51.100.0/24")
            node_ips = {
                a.address for n in (await v1_nodes(k7_core)).items for a in n.status.addresses or [] if "IP" in a.type
            }
            assert str(observed) not in node_ips, (
                f"the pod saw the node IP {observed} — node-local traffic is no longer SNAT'd, so this test and "
                "the BACKENDS.md caveat both need rewriting"
            )
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_expose_without_matching_ingress_port_is_refused(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-exp-noingress",
            image="alpine:3.20",
            namespace=test_namespace,
            expose_ports=[8000],
        )
        result = await k7_core.create_sandbox(cfg)
        assert not result.success
        assert "[8000]" in result.error
        assert not _deployment_exists("integ-exp-noingress", test_namespace)
