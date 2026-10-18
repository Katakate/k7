"""k7.api.chart_hooks with mocked Kubernetes APIs."""

from __future__ import annotations

import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from k7.api import chart_hooks as h


def _404(*_a, **_kw):
    raise ApiException(status=404)


def _ds(ns="kube-system", name="cilium", ready=1, desired=1, generation=1, observed=1, containers=()):
    return NS(
        metadata=NS(namespace=ns, name=name, generation=generation),
        status=NS(number_ready=ready, desired_number_scheduled=desired, observed_generation=observed),
        spec=NS(template=NS(spec=NS(containers=list(containers)))),
    )


class Apis:
    """One AsyncMock per kubernetes_asyncio API class, patched into chart_hooks."""

    def __init__(self):
        self.ext, self.apps, self.core = AsyncMock(), AsyncMock(), AsyncMock()
        self.storage, self.custom = AsyncMock(), AsyncMock()
        self.core.read_namespace.return_value = NS(
            metadata=NS(labels={"pod-security.kubernetes.io/enforce": "privileged"})
        )

    def __enter__(self):
        self._patches = [
            patch.object(h.client, "ApiextensionsV1Api", return_value=self.ext),
            patch.object(h.client, "AppsV1Api", return_value=self.apps),
            patch.object(h.client, "CoreV1Api", return_value=self.core),
            patch.object(h.client, "StorageV1Api", return_value=self.storage),
            patch.object(h.client, "CustomObjectsApi", return_value=self.custom),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


CILIUM_OK = {"enable-l7-proxy": "true", "tofqdns-dns-reject-response-code": "nameError"}


async def test_cilium_ok():
    with Apis() as a:
        a.apps.list_daemon_set_for_all_namespaces.return_value = NS(items=[_ds()])
        a.core.read_namespaced_config_map.return_value = NS(data=CILIUM_OK)
        await h.preflight(None, "k7-system", "cilium", ["k7d"])


@pytest.mark.parametrize("code", [None, "refused"])
async def test_cilium_dns_reject_code_must_be_nxdomain(code):
    with Apis() as a:
        a.apps.list_daemon_set_for_all_namespaces.return_value = NS(items=[_ds()])
        data = {"enable-l7-proxy": "true"} | ({"tofqdns-dns-reject-response-code": code} if code else {})
        a.core.read_namespaced_config_map.return_value = NS(data=data)
        with pytest.raises(h.HookError, match="dnsRejectResponseCode=nameError"):
            await h.preflight(None, "k7-system", "cilium", ["k7d"])


async def test_cilium_refusals_are_listed_together():
    with Apis() as a:
        a.ext.read_custom_resource_definition.side_effect = _404
        a.apps.list_daemon_set_for_all_namespaces.return_value = NS(items=[_ds(ready=0)])
        a.core.read_namespaced_config_map.return_value = NS(data={"enable-l7-proxy": "false"})
        a.core.read_namespace.return_value = NS(metadata=NS(labels={}))
        with pytest.raises(h.HookError) as e:
            await h.preflight(None, "k7-system", "cilium", ["k7d"])
    msg = str(e.value)
    for want in ("ciliumnetworkpolicies.cilium.io", "is not Ready (0/1)", "enable-l7-proxy", "enforce=<unset>"):
        assert want in msg


async def test_kubernetes_provider_needs_an_engine():
    with Apis() as a:
        a.apps.list_daemon_set_for_all_namespaces.return_value = NS(items=[])
        with pytest.raises(h.HookError, match="no NetworkPolicy engine runs"):
            await h.preflight(None, "k7-system", "kubernetes", ["k7d"])


async def test_aws_node_counts_only_with_policy_agent():
    agent = NS(name="aws-network-policy-agent", args=["--enable-network-policy=true"])
    with Apis() as a:
        a.apps.list_daemon_set_for_all_namespaces.side_effect = lambda label_selector: NS(
            items=[_ds(containers=[agent])] if label_selector == "k8s-app=aws-node" else []
        )
        assert await h.check_policy_engine(None) == []
        agent.args = ["--enable-network-policy=false"]
        assert "VPC CNI add-on" in (await h.check_policy_engine(None))[0]


async def test_kql_storage(monkeypatch):
    monkeypatch.setenv("K7_KQL_STORAGE_CLASS", "ebs-gp3")
    monkeypatch.setenv("K7_KQL_SNAPSHOT_CLASS", "ebs-snap")
    with Apis() as a:
        a.storage.read_storage_class.side_effect = _404
        with pytest.raises(h.HookError, match="StorageClass 'ebs-gp3'"):
            await h.preflight(None, "k7-system", "none", ["kql"])
        a.storage.read_storage_class.side_effect = None
        a.custom.get_cluster_custom_object.side_effect = _404
        with pytest.raises(h.HookError, match="VolumeSnapshotClass 'ebs-snap'"):
            await h.preflight(None, "k7-system", "none", ["kql"])


async def test_ensure_secrets_creates_missing_and_keeps_existing():
    with Apis() as a:
        existing = {h.AGENT_TOKEN_SECRET: NS(data={"agent_token": "dG9r"})}
        a.core.read_namespaced_secret.side_effect = lambda name, ns: existing[name] if name in existing else _404()
        await h.ensure_secrets(None, "k7-system")
    created = [c.args[1].metadata.name for c in a.core.create_namespaced_secret.call_args_list]
    assert created == [h.KEYS_SECRET, h.USAGE_SECRET]


async def test_ensure_secrets_refuses_token_secret_without_key():
    with Apis() as a:
        a.core.read_namespaced_secret.return_value = NS(data={})
        with pytest.raises(h.HookError, match="without an agent_token key"):
            await h.ensure_secrets(None, "k7-system")


async def test_ensure_cluster_id_creates_once_and_refuses_a_non_uuid():
    with Apis() as a:
        a.core.read_namespaced_config_map.side_effect = _404
        await h.ensure_cluster_id(None, "k7-system")
        (call,) = a.core.create_namespaced_config_map.call_args_list
        assert call.args[0] == "k7-system" and call.args[1].metadata.name == h.CLUSTER_ID_CM
        assert str(uuid.UUID(call.args[1].data["id"])) == call.args[1].data["id"]
        a.core.read_namespaced_config_map.side_effect = None
        a.core.read_namespaced_config_map.return_value = NS(data={"id": call.args[1].data["id"]})
        await h.ensure_cluster_id(None, "k7-system")
        assert a.core.create_namespaced_config_map.call_count == 1
        a.core.read_namespaced_config_map.return_value = NS(data={"id": "not-a-uuid"})
        with pytest.raises(h.HookError, match="no UUID"):
            await h.ensure_cluster_id(None, "k7-system")


async def test_install_with_remote_install_off_unmarks_nodes_keeps_the_pool_and_drops_records():
    ri = h.remote_install
    with Apis() as a:
        a.core.list_node.return_value = NS(items=[NS(metadata=NS(name="n1"))])
        a.core.list_namespaced_config_map.return_value = NS(items=[NS(metadata=NS(name="k7-install-0"))])
        await h.converge_remote_install(None, "k7", "0")
    a.core.list_node.assert_awaited_once_with(label_selector=f"{ri.ADDED_BY_LABEL}={ri.ADDED_BY}")
    a.core.patch_node.assert_awaited_once_with("n1", {"metadata": {"labels": {ri.ADDED_BY_LABEL: None}}})
    a.core.list_namespaced_config_map.assert_awaited_once_with("k7", label_selector=ri.RECORD_SELECTOR)
    a.core.delete_namespaced_config_map.assert_awaited_once_with("k7-install-0", "k7")


async def test_install_with_remote_install_on_touches_nothing():
    with Apis() as a:
        await h.converge_remote_install(None, "k7", "1")
    assert a.core.method_calls == []


async def test_install_refuses_an_unknown_remote_install_value():
    with Apis(), pytest.raises(h.HookError, match="expected 0 or 1"):
        await h.converge_remote_install(None, "k7", "true")


async def test_install_hook_runs_preflight_secrets_then_converges(monkeypatch):
    monkeypatch.setenv("K7_HOOK_TIMEOUT_S", "60")
    monkeypatch.setenv("K7_HOOK_PROVIDER", "cilium")
    monkeypatch.setenv("K7_HOOK_BACKENDS", "k7d")
    monkeypatch.setenv("K7_HOOK_REMOTE_INSTALL", "0")
    order: list[str] = []
    with (
        patch.object(h.config, "load_incluster_config"),
        patch.object(h, "preflight", AsyncMock(side_effect=lambda *a: order.append("preflight"))),
        patch.object(h, "ensure_secrets", AsyncMock(side_effect=lambda *a: order.append("secrets"))),
        patch.object(h, "ensure_cluster_id", AsyncMock(side_effect=lambda *a: order.append("cluster-id"))),
        patch.object(h, "converge_remote_install", AsyncMock(side_effect=lambda *a: order.append(a[2]))),
    ):
        await h.run("install")
    assert order == ["preflight", "secrets", "cluster-id", "0"]


async def test_uninstall_wait_deletes_secrets_once_cleanup_ready():
    with Apis() as a:
        a.core.list_node.side_effect = lambda label_selector: NS(
            items=[NS(metadata=NS(name="n1"))] if label_selector == h.POOL_SELECTOR else []
        )
        a.apps.read_namespaced_daemon_set.return_value = _ds(name=h.CLEANUP_DS)
        await h.uninstall_wait(None, "k7-system", 60)
    assert [c.args[0] for c in a.core.delete_namespaced_secret.call_args_list] == list(h.HOOK_SECRETS)


async def test_uninstall_wait_fails_when_nodes_keep_ready_label():
    with Apis() as a:
        a.core.list_node.return_value = NS(items=[NS(metadata=NS(name="n1"))])
        a.apps.read_namespaced_daemon_set.return_value = _ds(name=h.CLEANUP_DS)
        with pytest.raises(h.HookError, match="still carry k7.katakate.org/node-ready: n1"):
            await h.uninstall_wait(None, "k7-system", 60)
    a.core.delete_namespaced_secret.assert_not_called()


async def test_uninstall_wait_times_out_with_report():
    pod = NS(
        metadata=NS(name="k7-node-cleanup-x"),
        spec=NS(node_name="n1"),
        status=NS(phase="Running", container_statuses=[NS(state=NS(waiting=None, running=True))]),
    )
    with Apis() as a:
        a.core.list_node.return_value = NS(items=[NS(metadata=NS(name="n1"))])
        a.core.list_namespaced_pod.return_value = NS(items=[pod])
        a.apps.read_namespaced_daemon_set.return_value = _ds(name=h.CLEANUP_DS, ready=0)
        with pytest.raises(h.HookError, match="k7-node-cleanup-x on n1: Running running.*forceCleanup"):
            await h.uninstall_wait(None, "k7-system", 0)


async def test_cilium_without_agent_daemonset():
    with Apis() as a:
        a.apps.list_daemon_set_for_all_namespaces.return_value = NS(items=[])
        problems = await h.check_cilium(None)
    assert problems == ["no Cilium agent DaemonSet (label k8s-app=cilium) in any namespace"]


async def test_cilium_without_config_map_cannot_confirm_dns_proxy():
    with Apis() as a:
        a.apps.list_daemon_set_for_all_namespaces.return_value = NS(items=[_ds()])
        a.core.read_namespaced_config_map.side_effect = _404
        problems = await h.check_cilium(None)
    assert problems == ["ConfigMap kube-system/cilium-config is missing: cannot confirm Cilium's DNS proxy"]


async def test_cilium_config_map_errors_are_loud():
    with Apis() as a:
        a.apps.list_daemon_set_for_all_namespaces.return_value = NS(items=[_ds()])
        a.core.read_namespaced_config_map.side_effect = ApiException(status=403)
        with pytest.raises(ApiException):
            await h.check_cilium(None)


async def test_kubernetes_provider_accepts_any_known_engine():
    with Apis() as a:
        a.apps.list_daemon_set_for_all_namespaces.side_effect = lambda label_selector: NS(
            items=[_ds()] if label_selector == "k8s-app=calico-node" else []
        )
        await h.preflight(None, "k7-system", "kubernetes", ["k7d"])


async def test_kql_storage_lists_every_missing_crd_and_skips_class_lookups():
    with Apis() as a:
        a.ext.read_custom_resource_definition.side_effect = _404
        a.storage.read_storage_class.side_effect = _404
        problems = await h.check_kql_storage(None, "sc", "vsc")
    assert [p.split()[1] for p in problems[:3]] == list(h.SNAPSHOT_CRDS)
    assert "StorageClass 'sc'" in problems[3] and len(problems) == 4
    a.custom.get_cluster_custom_object.assert_not_called()


async def test_ensure_secrets_generates_a_random_agent_token():
    with Apis() as a:
        a.core.read_namespaced_secret.side_effect = _404
        await h.ensure_secrets(None, "k7-system")
    bodies = {c.args[1].metadata.name: c.args[1] for c in a.core.create_namespaced_secret.call_args_list}
    assert set(bodies) == set(h.HOOK_SECRETS)
    token = bodies[h.AGENT_TOKEN_SECRET].string_data["agent_token"]
    assert len(token) == 64 and int(token, 16) >= 0
    assert bodies[h.KEYS_SECRET].string_data is None


async def test_uninstall_stop_deletes_installer_and_waits_for_pods(monkeypatch):
    monkeypatch.setattr(h.asyncio, "sleep", AsyncMock())
    with Apis() as a:
        a.core.list_namespaced_pod.side_effect = [NS(items=[NS()]), NS(items=[])]
        a.apps.read_namespaced_daemon_set.side_effect = _404
        await h.uninstall_stop(None, "k7-system", 60)
    a.apps.delete_namespaced_daemon_set.assert_awaited_once_with(
        h.INSTALLER_DS, "k7-system", propagation_policy="Foreground"
    )
    assert a.core.list_namespaced_pod.await_args.kwargs == {"label_selector": f"app={h.INSTALLER_DS}"}


async def test_uninstall_stop_tolerates_an_already_deleted_installer(monkeypatch):
    with Apis() as a:
        a.apps.delete_namespaced_daemon_set.side_effect = _404
        a.core.list_namespaced_pod.return_value = NS(items=[])
        a.apps.read_namespaced_daemon_set.side_effect = _404
        await h.uninstall_stop(None, "k7-system", 60)


async def test_uninstall_stop_times_out_loudly():
    with Apis() as a:
        a.core.list_namespaced_pod.return_value = NS(items=[NS(), NS()])
        with pytest.raises(h.HookError, match="k7-node-installer still has 2 pod"):
            await h.uninstall_stop(None, "k7-system", 0)


async def test_uninstall_stop_other_delete_errors_are_loud():
    with Apis() as a:
        a.apps.delete_namespaced_daemon_set.side_effect = ApiException(status=403)
        with pytest.raises(ApiException):
            await h.uninstall_stop(None, "k7-system", 60)


async def test_uninstall_wait_waits_for_the_new_generation(monkeypatch):
    """An unsynced DaemonSet status describes the previous spec: never trust it."""
    monkeypatch.setattr(h.asyncio, "sleep", AsyncMock())
    with Apis() as a:
        a.core.list_node.side_effect = lambda label_selector: NS(
            items=[NS(metadata=NS(name="n1"))] if label_selector == h.POOL_SELECTOR else []
        )
        a.apps.read_namespaced_daemon_set.side_effect = [
            _ds(name=h.CLEANUP_DS, generation=2, observed=1),
            _ds(name=h.CLEANUP_DS, ready=0),
            _ds(name=h.CLEANUP_DS),
        ]
        await h.uninstall_wait(None, "k7-system", 60)
    assert a.apps.read_namespaced_daemon_set.await_count == 3
    assert a.core.delete_namespaced_secret.await_count == len(h.HOOK_SECRETS)


async def test_uninstall_wait_with_an_empty_pool_deletes_secrets():
    with Apis() as a:
        a.core.list_node.return_value = NS(items=[])
        a.apps.read_namespaced_daemon_set.return_value = _ds(name=h.CLEANUP_DS, ready=0, desired=0)
        a.core.delete_namespaced_secret.side_effect = _404
        await h.uninstall_wait(None, "k7-system", 60)


class _FakeK7:
    def __init__(self, fail: str = ""):
        self.fail = fail
        self.calls: list[tuple] = []

    def _r(self, what):
        self.calls.append(what)
        ok = what[0] != self.fail
        return NS(success=ok, error=None if ok else f"{what[0]} broke", message="")

    async def create_sandbox(self, cfg):
        return self._r(("create", cfg.name, cfg.backend, cfg.image))

    async def fork_sandbox(self, name, new, namespace):
        return self._r(("fork", name, new))

    async def delete_sandbox(self, name, ns):
        return self._r(("delete", name))

    async def exec_command(self, name, cmd, namespace):
        self.calls.append(("exec", name))
        return NS(exit_code=0, stdout="k7-helm-test\n", stderr="")

    async def aclose(self):
        self.calls.append(("aclose",))


def _smoke_env(k7: _FakeK7):
    api = AsyncMock()
    v1 = AsyncMock()
    return (
        (
            patch("k7.core.core.K7Core", return_value=k7),
            patch.object(h.config, "load_incluster_config"),
            patch.object(h.client, "ApiClient", return_value=api),
            patch.object(h.client, "CoreV1Api", return_value=v1),
        ),
        v1,
        api,
    )


async def test_smoke_forks_only_forkable_backends():
    k7 = _FakeK7()
    patches, v1, api = _smoke_env(k7)
    with patches[0], patches[1], patches[2], patches[3]:
        await h.smoke(["kfd", "k7d-fc"], 10)
    assert k7.calls == [
        ("create", "t-kfd", "kata-firecracker-devmapper", h.TEST_IMAGE),
        ("exec", "t-kfd"),
        ("delete", "t-kfd"),
        ("create", "t-k7d-fc", "k7d-fc", h.TEST_IMAGE),
        ("exec", "t-k7d-fc"),
        ("fork", "t-k7d-fc", "t-k7d-fc-fork"),
        ("exec", "t-k7d-fc-fork"),
        ("delete", "t-k7d-fc-fork"),
        ("delete", "t-k7d-fc"),
        ("aclose",),
    ]
    ns = v1.create_namespace.await_args.args[0].metadata.name
    assert ns.startswith("k7-helm-test-")
    v1.delete_namespace.assert_awaited_once_with(ns)
    api.close.assert_awaited_once()


async def test_smoke_failure_still_deletes_its_namespace():
    k7 = _FakeK7(fail="fork")
    patches, v1, api = _smoke_env(k7)
    with patches[0], patches[1], patches[2], patches[3], pytest.raises(h.HookError, match="fork kql: fork broke"):
        await h.smoke(["kql"], 10)
    v1.delete_namespace.assert_awaited_once()
    assert k7.calls[-1] == ("aclose",)
    api.close.assert_awaited_once()


async def test_wait_exec_times_out_with_the_last_error(monkeypatch):
    monkeypatch.setattr(h.asyncio, "sleep", AsyncMock())
    k7 = NS(exec_command=AsyncMock(return_value=NS(exit_code=1, stdout="", stderr="no such vm")))
    clock = iter([0.0, 0.0, 1.0, 99.0])
    monkeypatch.setattr(h, "time", NS(monotonic=lambda: next(clock)))
    with pytest.raises(h.HookError, match="never succeeded within 5s: exit 1: no such vm"):
        await h._wait_exec(k7, "sb", "ns", 5)


@pytest.mark.parametrize(
    ("command", "called"),
    [
        ("install", ["preflight", "ensure_secrets", "ensure_cluster_id", "converge_remote_install"]),
        ("uninstall-stop", ["uninstall_stop"]),
        ("uninstall-wait", ["uninstall_wait"]),
    ],
)
async def test_run_dispatches_in_the_system_namespace(monkeypatch, command, called):
    monkeypatch.setenv("K7_HOOK_TIMEOUT_S", "42")
    monkeypatch.setenv("K7_HOOK_PROVIDER", "cilium")
    monkeypatch.setenv("K7_HOOK_BACKENDS", "kql,k7d")
    monkeypatch.setenv("K7_SYSTEM_NAMESPACE", "k7-system")
    monkeypatch.setenv("K7_HOOK_REMOTE_INSTALL", "0")
    names = ("preflight", "ensure_secrets", "ensure_cluster_id", "converge_remote_install", "uninstall_stop")
    fns = {n: AsyncMock() for n in (*names, "uninstall_wait")}
    api = AsyncMock()
    api.__aenter__.return_value = api
    with (
        patch.object(h.config, "load_incluster_config"),
        patch.object(h.client, "ApiClient", return_value=api),
        patch.multiple(h, **fns),
    ):
        await h.run(command)
    assert [n for n, f in fns.items() if f.await_count] == called
    if command == "install":
        fns["preflight"].assert_awaited_once_with(api, "k7-system", "cilium", ["kql", "k7d"])
        fns["converge_remote_install"].assert_awaited_once_with(api, "k7-system", "0")
    else:
        assert fns[called[0]].await_args.args == (api, "k7-system", 42.0)


async def test_run_test_runs_smoke_over_hook_backends(monkeypatch):
    monkeypatch.setenv("K7_HOOK_TIMEOUT_S", "7")
    monkeypatch.setenv("K7_HOOK_BACKENDS", "k7d,k7d-fc")
    with patch.object(h, "smoke", AsyncMock()) as smoke:
        await h.run("test")
    smoke.assert_awaited_once_with(["k7d", "k7d-fc"], 7.0)


def test_main_returns_1_on_hook_error(monkeypatch, capsys):
    monkeypatch.setenv("K7_HOOK_TIMEOUT_S", "1")
    with patch.object(h, "run", AsyncMock(side_effect=h.HookError("refusing to install k7"))):
        assert h.main(["install"]) == 1
    assert "k7 chart hook install: FAILED: refusing to install k7" in capsys.readouterr().err
    with patch.object(h, "run", AsyncMock()):
        assert h.main(["test"]) == 0


def test_main_reports_missing_env(monkeypatch, capsys):
    monkeypatch.delenv("K7_HOOK_TIMEOUT_S", raising=False)
    assert h.main(["install"]) == 1
    assert "K7_HOOK_TIMEOUT_S is not set" in capsys.readouterr().err
