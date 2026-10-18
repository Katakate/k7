"""The Helm chart renders over the value matrix, refuses unsafe
values, and stays in parity with the K3s playbook manifests it replaces."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from k7 import __version__
from k7.deploy import images as k7_images
from k7.deploy import k7_api_tls
from k7.deploy.node_installer import entrypoint
from k7.metering import config as metering_config

REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "deploy/helm/k7"
BASE = CHART / "ci/base-values.yaml"
DEPLOY = REPO / "src/k7/deploy"
MANIFESTS = DEPLOY / "manifests"
HELM_VERSION = "v3.16.4"
KUBE_VERSION = "1.36.5"
MATRIX = yaml.safe_load((CHART / "ci/matrix.yaml").read_text())
BACKEND_NAMES = {"kfd": "kata-firecracker-devmapper", "kql": "kata-qemu-longhorn", "k7d": "k7d", "k7dFc": "k7d-fc"}
INSTALLER_NAMES = {"kfd": "kfd", "kql": "kql", "k7d": "k7d", "k7dFc": "k7d-fc"}
CILIUM_KINDS = {"CiliumNetworkPolicy", "CiliumClusterwideNetworkPolicy"}


def _helm_or_skip() -> str:
    helm = shutil.which("helm")
    found = subprocess.run([helm, "version", "--short"], capture_output=True, text=True).stdout if helm else ""
    if helm and found.startswith(HELM_VERSION + "+"):
        return helm
    reason = f"helm {HELM_VERSION} not installed (found {found.strip() or 'none'})"
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([_helm_or_skip(), *args], capture_output=True, text=True, cwd=CHART)


def _set_args(sets: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k, v in sets.items():
        out += [
            "--set-json" if isinstance(v, (list, dict)) else "--set",
            f"{k}={json.dumps(v) if isinstance(v, (list, dict)) else v}",
        ]
    return out


def _template(
    sets: dict[str, Any], namespace: str = "k7-system", base: Path = BASE, extra: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    return _helm(
        "template",
        "k7",
        ".",
        "-n",
        namespace,
        "--kube-version",
        KUBE_VERSION,
        "-f",
        str(base),
        *_set_args(sets),
        *extra,
    )


def render(sets: dict[str, Any], namespace: str = "k7-system", extra: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    r = _template(sets, namespace, extra=extra)
    assert r.returncode == 0, r.stderr
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def _provider_sets(provider: str) -> dict[str, Any]:
    sets: dict[str, Any] = {"networkPolicy.provider": provider}
    if provider != "cilium":
        sets["networkPolicy.acknowledgeNoPlatformDeny"] = "true"
    if provider == "none":
        sets["networkPolicy.acknowledgeNoEgressPolicy"] = "true"
        sets["observability.metrics"] = "false"
    return sets


def _sets(provider: str = "cilium", backends: tuple[str, ...] = ("kql", "k7d"), tls: str = "secret") -> dict[str, Any]:
    return {**_provider_sets(provider), "tls.mode": tls, **{f"backends.{b}.enabled": "true" for b in backends}}


def find(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    hits = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(hits) == 1, f"expected one {kind}/{name}, got {len(hits)}"
    return hits[0]


def _load(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _check_invariants(docs: list[dict[str, Any]], provider: str, backends: tuple[str, ...]) -> None:
    kinds = {d["kind"] for d in docs}
    assert kinds & CILIUM_KINDS == (CILIUM_KINDS if provider == "cilium" else set())
    assert ("CronJob" in kinds) == ("kql" in backends)
    for node in _walk(docs):
        if "image" in node and isinstance(node["image"], str):
            assert re.search(r"@sha256:[0-9a-f]{64}$", node["image"]) or node["image"] == "alpine:3.20", node["image"]
        assert node.get("imagePullPolicy") != "Never"
        assert node.get("hostPath", {}).get("path") not in ("/etc/k7", "/etc/k7/")


MATRIX_CASES = [
    (p, (b,), t) for p, b, t in itertools.product(MATRIX["providers"], MATRIX["backends"], MATRIX["tlsModes"])
] + [(p, tuple(MATRIX["backends"]), "secret") for p in MATRIX["providers"]]


@pytest.mark.parametrize(("provider", "backends", "tls"), MATRIX_CASES)
def test_render_matrix(provider: str, backends: tuple[str, ...], tls: str) -> None:
    sets = _sets(provider, backends, tls)
    if backends == ("kfd",):
        sets["nodeInstaller.diskSource"] = "spare-disk"
    docs = render(sets)
    _check_invariants(docs, provider, backends)
    host_cfg = find(docs, "ConfigMap", "k7-host-config")["data"]
    assert host_cfg["backend"] == BACKEND_NAMES[backends[0]]
    assert ("k7d_version" in host_cfg) == bool({"k7d", "k7dFc"} & set(backends))
    cfg = json.loads(find(docs, "ConfigMap", "k7-node-installer")["data"]["config.json"])
    assert cfg["backends"] == [INSTALLER_NAMES[b] for b in MATRIX["backends"] if b in backends]
    entrypoint.validate_config(cfg)


def test_helm_lint() -> None:
    r = _helm(
        "lint",
        ".",
        "--kube-version",
        KUBE_VERSION,
        "-f",
        str(BASE),
        *_set_args(_sets(backends=tuple(MATRIX["backends"]))),
    )
    assert r.returncode == 0, r.stdout + r.stderr


# Digests the release pipeline fills; the RKE2 recipe passes them with --set.
_RKE2_DIGESTS = {
    "images.api.digest": "sha256:" + "1" * 64,
    "images.nodeInstaller.digest": "sha256:" + "2" * 64,
}
_RKE2_CILIUM = CHART / "ci/rke2-cilium-values.yaml"
_RKE2_KUBERNETES = CHART / "ci/rke2-kubernetes-values.yaml"


def _render_files(*files: Path, sets: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    args: list[str] = []
    for f in files:
        args += ["-f", str(f)]
    r = _helm("template", "k7", ".", "-n", "k7-system", "--kube-version", KUBE_VERSION, *args, *_set_args(sets or {}))
    assert r.returncode == 0, r.stderr
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def test_rke2_install_recipe_sets_mode_and_scraper() -> None:
    """rke2-node.sh chart: the documented values render with metrics on."""
    docs = _render_files(_RKE2_CILIUM, sets=_RKE2_DIGESTS)
    env = {
        e["name"]: e.get("value")
        for e in find(docs, "Deployment", "k7-api")["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["K7_API_MODE"] == "development"
    assert env["K7_METRICS_PORT"] == "9464"
    policy = find(docs, "CiliumNetworkPolicy", "k7-metrics")
    assert policy["spec"]["ingress"][0]["fromEndpoints"][0]["matchLabels"] == {
        "k8s:io.kubernetes.pod.namespace": "monitoring",
        "app.kubernetes.io/name": "prometheus",
    }
    overlay = _render_files(_RKE2_CILIUM, _RKE2_KUBERNETES, sets=_RKE2_DIGESTS)
    netpol = find(overlay, "NetworkPolicy", "k7-api-metrics")
    scraper = netpol["spec"]["ingress"][-1]["from"][0]
    assert scraper["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "monitoring"}
    assert scraper["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "prometheus"}


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"networkPolicy.provider": None}, "networkPolicy"),
        (
            {"networkPolicy.acknowledgeNoPlatformDeny": None, "networkPolicy.provider": "kubernetes"},
            "acknowledgeNoPlatformDeny",
        ),
        (
            {
                "networkPolicy.acknowledgeNoEgressPolicy": None,
                "networkPolicy.provider": "none",
                "networkPolicy.acknowledgeNoPlatformDeny": "true",
            },
            "acknowledgeNoEgressPolicy",
        ),
        ({"backends.kql.storageClass": ""}, "storageClass"),
        ({"backends.kql.volumeSnapshotClass": ""}, "volumeSnapshotClass"),
        ({"tls.mode": None}, "mode"),
        ({"networkPolicy.bogus": "true"}, "bogus"),
        ({"backends.kql.enabled": "false", "backends.k7d.enabled": "false"}, "backend"),
        ({"nodeInstaller.distro": None}, "distro"),
        ({"images.api.digest": "latest"}, "digest"),
        ({"tls.mode": "certManager", "tls.certManager.issuerRef.name": ""}, "issuerRef"),
        ({"networkPolicy.apiAllowCIDRs": ["10.0.0.0/8"]}, "apiAllowCIDRs"),
        ({"observability.scraperSelector": None}, "scraperSelector"),
        (
            {
                "networkPolicy.provider": "none",
                "networkPolicy.acknowledgeNoPlatformDeny": "true",
                "networkPolicy.acknowledgeNoEgressPolicy": "true",
            },
            "9464",
        ),
        ({"observability.serviceMonitor": "true"}, "ServiceMonitor and PodMonitor CRDs"),
        ({"observability.prometheusRule": "true"}, "PrometheusRule CRD"),
        ({"observability.logLevel": "verbose"}, "logLevel"),
        ({"observability.thresholds.apiErrorRatio": "2"}, "apiErrorRatio"),
        ({"observability.scraperSelector.podLabels": None}, "podLabels"),
        ({"api.mode": None}, "mode"),
        ({"api.mode": "staging"}, "mode"),
        ({"api.mode": "production", "tls.mode": "insecureHTTP"}, "insecureHTTP"),
        ({"api.mode": "production", "api.remoteInstall": "true"}, "api.mode=production refuses api.remoteInstall=true"),
    ],
)
def test_render_refuses(change: dict[str, Any], error: str) -> None:
    sets = {k: v for k, v in {**_sets(), **change}.items() if v is not None}
    unset = [k for k, v in change.items() if v is None]
    if unset:
        # Drop the key from the base file too, so the value is absent (not "").
        base = yaml.safe_load(BASE.read_text())
        for key in unset:
            parent, _, leaf = key.rpartition(".")
            node = base
            for part in parent.split("."):
                node = node.get(part, {})
            node.pop(leaf, None)
        tmp = CHART.parent / ".test-base-values.yaml"
        tmp.write_text(yaml.safe_dump(base))
        try:
            r = _template(sets, base=tmp)
        finally:
            tmp.unlink()
    else:
        r = _template(sets)
    assert r.returncode != 0, f"rendered despite {change}"
    assert error in r.stderr, r.stderr


# --- parity with the playbook (the K3s path) --------------------------------


def _playbook_docs(name: str) -> list[dict[str, Any]]:
    return _load(MANIFESTS / name)


def test_api_clusterrole_matches_playbook() -> None:
    docs = render(_sets())
    assert find(docs, "ClusterRole", "k7-api")["rules"] == _playbook_docs("k7-api/clusterrole.yaml")[0]["rules"]


def test_api_keys_role_matches_playbook() -> None:
    docs = render(_sets())
    want = next(d for d in _playbook_docs("k7-api/role.yaml") if d["kind"] == "Role")
    assert find(docs, "Role", "k7-api-keys")["rules"] == want["rules"]


def test_tenant_crd_matches_playbook() -> None:
    assert (CHART / "crds/crd-tenants.yaml").read_text() == (MANIFESTS / "k7-api/crd-tenants.yaml").read_text()


@pytest.mark.parametrize(
    ("mode", "tls", "transport"), [("production", "secret", "https"), ("development", "insecureHTTP", "insecure-http")]
)
def test_api_mode_and_transport_env(mode: str, tls: str, transport: str) -> None:
    dep = find(render({**_sets(tls=tls), "api.mode": mode, "metering.sampleInterval": "15"}), "Deployment", "k7-api")
    env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert (env["K7_API_MODE"], env["K7_API_TRANSPORT"]) == (mode, transport)


DEPLOY_TIME_ENV = {"K7_API_ENABLE_REMOTE_INSTALL", "K7_NODE_INSTALLER_IMAGE", "K7_NODE_INSTALLER_NAMESPACE"}


def _strip_image(container: dict[str, Any]) -> dict[str, Any]:
    """Drop what each path fills in at deploy time: image, pull policy, and the remote-install env values."""
    out = {k: v for k, v in container.items() if k not in ("image", "imagePullPolicy")}
    if "env" in out:
        out["env"] = [{"name": e["name"]} if e["name"] in DEPLOY_TIME_ENV else e for e in out["env"]]
    return out


def test_api_containers_match_playbook_tls() -> None:
    docs = render(_sets())
    dep = find(docs, "Deployment", "k7-api")
    caddyfile = k7_api_tls.render_caddyfile(acme=False)
    metering = find(docs, "ConfigMap", "k7-metering")["data"]["metering.json"]
    playbook = yaml.safe_load(
        k7_api_tls.render_deployment(
            (MANIFESTS / "k7-api/deployment.yaml")
            .read_text()
            .replace("__K7_OIDC_CONFIG_HASH__", k7_images.OIDC_OFF_HASH)
            .replace("__K7_METERING_CONFIG_HASH__", hashlib.sha256(metering.encode()).hexdigest()[:16])
            .replace("__K7_API_MODE__", "development")
            .replace("__K7_API_TRANSPORT__", "https"),
            acme=False,
            caddyfile=caddyfile,
        )
    )
    chart_pod, play_pod = dep["spec"]["template"], playbook["spec"]["template"]
    sources = []
    for pod in (chart_pod, play_pod):
        env = pod["spec"]["containers"][0]["env"]
        sources.append(next(e for e in env if e["name"] == "K7_TLS_CERT_SOURCE").pop("value"))
    assert sources == ["secret", "files"], "k7_tls_cert_expiry_timestamp_seconds{source} names each path's cert"
    assert [_strip_image(c) for c in chart_pod["spec"]["containers"]] == [
        _strip_image(c) for c in play_pod["spec"]["containers"]
    ]
    assert chart_pod["metadata"]["annotations"] == play_pod["metadata"]["annotations"]
    assert find(docs, "ConfigMap", "k7-api-caddyfile")["data"]["Caddyfile"] == caddyfile
    assert dep["spec"]["template"]["spec"]["containers"][1]["image"] == k7_api_tls.CADDY_PIN.replace(":2.10.2@", "@")
    assert "affinity" not in chart_pod["spec"], "chart has no first-master label to prefer"


def test_api_container_without_kql_drops_storage_env() -> None:
    env = find(render(_sets(backends=("k7d",))), "Deployment", "k7-api")["spec"]["template"]["spec"]["containers"][0][
        "env"
    ]
    assert {e["name"] for e in env} == {
        "K7_API_KEY_STORE",
        "K7_SYSTEM_NAMESPACE",
        "K7_K7D_REMOTE_ONLY",
        "K7_API_MODE",
        "K7_API_TRANSPORT",
        "K7_EGRESS_MODE",
        "K7_TLS_CERT_SOURCE",
        "K7_TLS_SNI",
        "K7_LOG_FORMAT",
        "K7_LOG_LEVEL",
        "K7_METRICS_PORT",
        *DEPLOY_TIME_ENV,
        "K7_NODE_NAME",
        "K7_METERING_CONFIG",
        "K7_CLUSTER_ID",
    }


def _api_env(docs: list[dict[str, Any]]) -> dict[str, Any]:
    (api, *_) = find(docs, "Deployment", "k7-api")["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value") for e in api["env"]}


def test_remote_install_is_off_by_default() -> None:
    docs = render(_sets())
    env = _api_env(docs)
    assert (
        env["K7_API_ENABLE_REMOTE_INSTALL"],
        env["K7_NODE_INSTALLER_IMAGE"],
        env["K7_NODE_INSTALLER_NAMESPACE"],
    ) == (
        "0",
        "",
        "",
    )
    assert not [d for d in docs if d["metadata"]["name"] == "k7-api-remote-install"]


def test_remote_install_pins_the_chart_installer_image() -> None:
    docs = render({**_sets(), "api.remoteInstall": "true"}, namespace="k7-prod")
    env = _api_env(docs)
    (installer,) = find(docs, "DaemonSet", "k7-node-installer")["spec"]["template"]["spec"]["containers"]
    assert env["K7_API_ENABLE_REMOTE_INSTALL"] == "1"
    assert env["K7_NODE_INSTALLER_IMAGE"] == installer["image"]
    assert re.search(r"@sha256:[0-9a-f]{64}$", installer["image"])
    assert env["K7_NODE_INSTALLER_NAMESPACE"] == "k7-prod"
    want_role, want_binding = _playbook_docs("k7-node-installer/k7-api-access/rbac.yaml")
    role, binding = find(docs, "Role", "k7-api-remote-install"), find(docs, "RoleBinding", "k7-api-remote-install")
    assert role["rules"] == want_role["rules"] and binding["roleRef"] == want_binding["roleRef"]
    assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == "k7-prod"
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "k7-api", "namespace": "k7-prod"}]


@pytest.mark.parametrize(("value", "env"), [("false", "0"), ("true", "1")])
def test_install_hook_converges_remote_install_off(value: str, env: str) -> None:
    docs = render({**_sets(), "api.remoteInstall": value})
    job = find(docs, "Job", "k7-preflight")
    (hook,) = job["spec"]["template"]["spec"]["containers"]
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert {e["name"]: e.get("value") for e in hook["env"]}["K7_HOOK_REMOTE_INSTALL"] == env
    rules = find(docs, "ClusterRole", "k7-hook")["rules"] + find(docs, "Role", "k7-hook")["rules"]
    assert {"apiGroups": [""], "resources": ["nodes"], "verbs": ["list", "patch"]} in rules
    assert {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["list", "delete"]} in rules
    # The installer stack is chart core (k7-agent shares its pool): off keeps it, only the API's access goes.
    find(docs, "DaemonSet", "k7-node-installer")
    find(docs, "ConfigMap", "k7-node-installer")


def test_remote_install_must_be_a_boolean() -> None:
    r = _template({**_sets(), "api.remoteInstall": "yes"})
    assert r.returncode != 0 and "remoteInstall" in r.stderr


@pytest.mark.parametrize(("provider", "mode"), [("cilium", "full"), ("kubernetes", "cidr-only"), ("none", "none")])
def test_egress_mode_follows_provider(provider: str, mode: str) -> None:
    docs = render(_sets(provider))
    for kind, name in (("Deployment", "k7-api"), ("CronJob", "k7-snapshot-gc")):
        obj = find(docs, kind, name)
        envs = [n for n in _walk(obj) if n.get("name") == "K7_EGRESS_MODE" and "value" in n]
        assert envs and all(e["value"] == mode for e in envs), (kind, envs)


def _vars(distro: str) -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / f"roles/k7_node_prep/vars/{distro}.yml").read_text())


@pytest.mark.parametrize("distro", ["k3s", "rke2", "kubeadm", "eks-al2023"])
def test_agent_matches_playbook(distro: str) -> None:
    docs = render({**_sets(), "nodeInstaller.distro": distro})
    socket = _vars(distro)["k7_containerd_socket"]
    text = (MANIFESTS / "k7-api/agent-daemonset.yaml").read_text()
    text = text.replace("__K7_CONTAINERD_SOCKET_DIR__", str(Path(socket).parent)).replace(
        "__K7_CONTAINERD_SOCKET__", socket
    )
    play = yaml.safe_load(text)["spec"]["template"]["spec"]
    chart = find(docs, "DaemonSet", "k7-agent")["spec"]["template"]["spec"]
    assert [_strip_image(c) for c in chart["containers"]] == [_strip_image(c) for c in play["containers"]]
    assert chart["volumes"] == play["volumes"]
    assert chart["nodeSelector"] == {"k7.katakate.org/node-pool": "true"}


def test_metering_config_is_what_k7_parses_and_rolls_both_pods() -> None:
    sink = {"name": "om", "type": "http", "url": "http://om:8888", "preset": "openmeter", "mode": "batch"}
    sets = {**_sets(), "metering.sampleInterval": "5", "metering.sinks": [{**sink, "batchSize": 50}]}
    docs = render(sets)
    raw = find(docs, "ConfigMap", "k7-metering")["data"]["metering.json"]
    cfg = metering_config.parse(json.loads(raw))
    assert (cfg.sample_interval, cfg.spool_max_bytes, cfg.sinks[0].url) == (5, 2**30, "http://om:8888/api/v1/events")
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    for kind, name in (("Deployment", "k7-api"), ("DaemonSet", "k7-agent")):
        assert (
            find(docs, kind, name)["spec"]["template"]["metadata"]["annotations"]["k7.katakate.org/metering-config"]
            == digest
        )
    assert metering_config.parse(json.loads(find(render(_sets()), "ConfigMap", "k7-metering")["data"]["metering.json"]))


def test_production_needs_an_explicit_sample_interval() -> None:
    r = _template({**_sets(), "api.mode": "production"})
    assert r.returncode != 0 and "metering.sampleInterval" in r.stderr
    assert render({**_sets(), "api.mode": "production", "metering.sampleInterval": "30"})


def test_cilium_policies_match_playbook() -> None:
    docs = render(_sets(), namespace="kube-system")
    deny = _playbook_docs("k7-api/cilium/sandbox-platform-deny.yaml")[0]
    assert find(docs, "CiliumClusterwideNetworkPolicy", deny["metadata"]["name"])["spec"] == deny["spec"]
    agent = _playbook_docs("k7-api/cilium/agent-networkpolicy.yaml")[0]
    agent["spec"]["ingress"][0]["fromEndpoints"].append({"matchLabels": {"app": "k7-helm-test"}})
    assert find(docs, "CiliumNetworkPolicy", agent["metadata"]["name"])["spec"] == agent["spec"]
    test_pod = find(docs, "Pod", "k7-test-sandboxes")
    assert test_pod["metadata"]["labels"] == {"app": "k7-helm-test"}
    other_ns = render(_sets())
    ns_list = json.dumps(find(other_ns, "CiliumClusterwideNetworkPolicy", deny["metadata"]["name"])["spec"])
    assert "k7-system" in ns_list, "the release namespace is a platform namespace"


def _playbook_runtimeclasses() -> dict[str, dict[str, Any]]:
    tasks = yaml.safe_load((DEPLOY / "roles/k7_cluster/tasks/nodes.yml").read_text())
    out = {}
    for t in tasks:
        script = (t.get("ansible.builtin.shell") or "") if isinstance(t, dict) else ""
        if "kind: RuntimeClass" in script:
            body = yaml.safe_load(script.split("<<'EOF'", 1)[1].split("\nEOF", 1)[0].split("\n", 1)[1])
            out[body["metadata"]["name"]] = body
    return out


def test_runtimeclasses_match_playbook() -> None:
    play = _playbook_runtimeclasses()
    docs = render(_sets(backends=tuple(MATRIX["backends"])))
    chart = {d["metadata"]["name"]: d for d in docs if d["kind"] == "RuntimeClass"}
    assert set(chart) == set(play)
    for name, rc in play.items():
        assert {k: v for k, v in chart[name].items() if k != "metadata"} == {
            k: v for k, v in rc.items() if k != "metadata"
        }


def test_installer_rbac_and_admission_match_playbook() -> None:
    docs = render(_sets())
    for path in ("k7-node-installer/admission-policy.yaml", "k7-node-installer/rbac.yaml"):
        for want in _playbook_docs(path):
            if want["kind"] == "ServiceAccount":
                continue
            got = find(docs, want["kind"], want["metadata"]["name"])
            assert {k: v for k, v in got.items() if k != "metadata"} == {
                k: v for k, v in want.items() if k != "metadata"
            }, want["metadata"]["name"]


def test_installer_config_change_rolls_daemonset() -> None:
    def sha(sets: dict[str, Any]) -> str:
        ds = find(render(sets), "DaemonSet", "k7-node-installer")
        return ds["spec"]["template"]["metadata"]["annotations"]["k7.katakate.org/installer-config-sha256"]

    assert sha(_sets()) == sha({**_sets(), "api.replicas": "3"})
    assert sha(_sets()) != sha({**_sets(), "nodeInstaller.allowServerRestart": "true"})


def test_versions_pinned_with_playbook() -> None:
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["version"] == chart["appVersion"] == __version__
    play_vars = yaml.safe_load((DEPLOY / "k7-install-node.yaml").read_text())[0]["vars"]
    assert chart["annotations"]["katakate.org/k7d-version"] == play_vars["k7d_version"]
    assert f"v{play_vars['k7d_version']}" in (DEPLOY / "node_installer/artifacts.txt").read_text()
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["images"]["caddy"]["digest"] == k7_api_tls.CADDY_DIGEST
    docs = render(_sets(backends=("k7d",)))
    assert find(docs, "ConfigMap", "k7-host-config")["data"]["k7d_version"] == play_vars["k7d_version"] + "\n"


# --- observability ---------------------------------------------

MONITORING_CRDS = tuple(
    arg
    for kind in ("ServiceMonitor", "PodMonitor", "PrometheusRule")
    for arg in ("--api-versions", f"monitoring.coreos.com/v1/{kind}")
)
OBSERVABILITY = DEPLOY / "observability"
SCRAPER = ("monitoring", {"app.kubernetes.io/name": "prometheus"})


@pytest.mark.parametrize("provider", ["cilium", "kubernetes"])
def test_metrics_policies_match_playbook(provider: str) -> None:
    from k7.deploy import k7_metrics_policy

    docs = render({**_sets(provider), "observability.scraperSelector.podLabels.team": "obs"}, namespace="kube-system")
    scraper = (SCRAPER[0], {**SCRAPER[1], "team": "obs"})
    if provider == "cilium":
        want = [k7_metrics_policy.cilium_policy("kube-system", scraper)]
    else:
        want = k7_metrics_policy.network_policies("kube-system", scraper, acme=False)
    for w in want:
        assert find(docs, w["kind"], w["metadata"]["name"]) == w


def test_metrics_off_drops_port_annotations_and_policies() -> None:
    docs = render({**_sets(), "observability.metrics": "false"})
    assert not [d for d in docs if d["metadata"]["name"] in ("k7-metrics", "k7-api-metrics", "k7-agent-metrics")]
    for kind, name in (("Deployment", "k7-api"), ("DaemonSet", "k7-agent")):
        pod = find(docs, kind, name)["spec"]["template"]
        assert "prometheus.io/scrape" not in (pod["metadata"].get("annotations") or {})
        container = pod["spec"]["containers"][0]
        assert 9464 not in [p["containerPort"] for p in container["ports"]]
        assert {"name": "K7_METRICS_PORT", "value": "off"} in container["env"]


def test_metrics_on_annotates_and_exposes_9464() -> None:
    docs = render({**_sets(), "observability.logLevel": "debug"})
    for kind, name in (("Deployment", "k7-api"), ("DaemonSet", "k7-agent")):
        pod = find(docs, kind, name)["spec"]["template"]
        assert pod["metadata"]["annotations"]["prometheus.io/port"] == "9464"
        container = pod["spec"]["containers"][0]
        assert {"name": "metrics", "containerPort": 9464, "protocol": "TCP"} in container["ports"]
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert (env["K7_LOG_FORMAT"], env["K7_LOG_LEVEL"], env["K7_METRICS_PORT"]) == ("json", "debug", "9464")
    gc = find(docs, "CronJob", "k7-snapshot-gc")["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "K7_LOG_FORMAT", "value": "json"} in gc["env"]


def test_monitors_render_with_crds() -> None:
    docs = render(
        {**_sets(), "observability.serviceMonitor": "true", "observability.monitorLabels.release": "kps"},
        extra=MONITORING_CRDS,
    )
    svc = find(docs, "Service", "k7-api-metrics")
    sm = find(docs, "ServiceMonitor", "k7-api")
    pm = find(docs, "PodMonitor", "k7-agent")
    assert svc["spec"]["selector"] == {"app": "k7-api"} and svc["spec"]["ports"][0]["port"] == 9464
    assert sm["spec"]["selector"]["matchLabels"].items() <= svc["metadata"]["labels"].items()
    assert svc["metadata"]["labels"]["app"] == "k7-api", "jobLabel app → job=k7-api in the alert rules"
    assert pm["spec"]["selector"]["matchLabels"] == {"app": "k7-agent"}
    for m in (sm, pm):
        assert m["spec"]["jobLabel"] == "app" and m["metadata"]["labels"] == {"release": "kps"}
    assert not [d for d in docs if d["kind"] == "PrometheusRule"]


def test_monitors_need_metrics() -> None:
    sets = {**_sets(), "observability.metrics": "false", "observability.prometheusRule": "true"}
    r = _template(sets, extra=MONITORING_CRDS)
    assert r.returncode != 0 and "need observability.metrics=true" in r.stderr, r.stderr


def _thresholds(group: dict[str, Any]) -> dict[tuple[str, str], float]:
    return {
        (r["record"], (r.get("labels") or {}).get("op", "")): float(r["expr"].removeprefix("vector(").rstrip(")"))
        for r in group["rules"]
    }


def test_prometheus_rule_matches_rules_file() -> None:
    rules = yaml.safe_load((OBSERVABILITY / "rules/k7.rules.yaml").read_text())["groups"]
    default = find(
        render({**_sets(), "observability.prometheusRule": "true"}, extra=MONITORING_CRDS), "PrometheusRule", "k7"
    )
    groups = default["spec"]["groups"]
    assert [g["name"] for g in groups] == ["k7.thresholds", "k7.alerts"]
    assert groups[1] == rules[1], "alerts ship verbatim"
    assert _thresholds(groups[0]) == _thresholds(rules[0]), "values.yaml defaults == the playbook's rules file"
    tuned = render(
        {
            **_sets(),
            "observability.prometheusRule": "true",
            "observability.thresholds.opP95BudgetSeconds.fork": "3",
            "observability.thresholds.tlsExpirySeconds": "86400",
        },
        extra=MONITORING_CRDS,
    )
    got = _thresholds(find(tuned, "PrometheusRule", "k7")["spec"]["groups"][0])
    assert got[("k7:threshold:op_p95_budget_seconds", "fork")] == 3
    assert got[("k7:threshold:tls_expiry_seconds", "")] == 86400


def test_dashboards_ship_as_configmaps() -> None:
    assert not [d for d in render(_sets()) if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-dashboard")]
    docs = render({**_sets(), "observability.grafanaDashboards": "true"})
    files = sorted((OBSERVABILITY / "dashboards").glob("*.json"))
    assert len(files) == 4
    for f in files:
        cm = find(docs, "ConfigMap", f"{f.stem}-dashboard")
        assert cm["metadata"]["labels"] == {"grafana_dashboard": "1"}
        assert cm["data"] == {f.name: f.read_text()}


def test_monitoring_monitors_match_chart():
    docs = render({**_sets(), "observability.serviceMonitor": "true"}, namespace="kube-system", extra=MONITORING_CRDS)
    for want in _load(OBSERVABILITY / "k7-monitors.yaml"):
        assert find(docs, want["kind"], want["metadata"]["name"]) == want


OIDC_SETS = {
    "auth.oidc.issuer": "https://idp.example.com/realms/k7",
    "auth.oidc.audience": "k7-api",
    "auth.oidc.clientId": "k7-cli",
    "auth.oidc.groupsClaim": "groups",
    "auth.oidc.usernameClaim": "email",
}


def _api_pod(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return find(docs, "Deployment", "k7-api")["spec"]["template"]


def test_oidc_off_by_default_has_no_configmap_but_optional_envfrom() -> None:
    docs = render(_sets())
    assert not [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "k7-oidc"]
    pod = _api_pod(docs)
    assert pod["metadata"]["annotations"]["k7.katakate.org/oidc-config"] == k7_images.OIDC_OFF_HASH
    assert pod["spec"]["containers"][0]["envFrom"] == [{"configMapRef": {"name": "k7-oidc", "optional": True}}]


def test_oidc_values_render_configmap_and_roll_the_pods() -> None:
    docs = render({**_sets(), **OIDC_SETS})
    assert find(docs, "ConfigMap", "k7-oidc")["data"] == {
        "K7_OIDC_ISSUER": "https://idp.example.com/realms/k7",
        "K7_OIDC_AUDIENCE": "k7-api",
        "K7_OIDC_CLIENT_ID": "k7-cli",
        "K7_OIDC_GROUPS_CLAIM": "groups",
        "K7_OIDC_USERNAME_CLAIM": "email",
        "K7_OIDC_JWKS_TTL_SECONDS": "3600",
    }
    on = _api_pod(docs)["metadata"]["annotations"]["k7.katakate.org/oidc-config"]
    other = _api_pod(render({**_sets(), **OIDC_SETS, "auth.oidc.audience": "other"}))
    assert on != k7_images.OIDC_OFF_HASH
    assert other["metadata"]["annotations"]["k7.katakate.org/oidc-config"] not in (on, k7_images.OIDC_OFF_HASH)


@pytest.mark.parametrize("missing", sorted(OIDC_SETS))
def test_partial_oidc_values_fail(missing: str) -> None:
    r = _template({**_sets(), **{k: v for k, v in OIDC_SETS.items() if k != missing}})
    assert r.returncode != 0 and "auth.oidc needs all of" in r.stderr


def test_http_oidc_issuer_is_rejected_by_schema() -> None:
    r = _template({**_sets(), **OIDC_SETS, "auth.oidc.issuer": "http://idp.example.com"})
    assert r.returncode != 0 and "issuer" in r.stderr


def test_short_oidc_jwks_ttl_is_rejected_by_schema() -> None:
    r = _template({**_sets(), **OIDC_SETS, "auth.oidc.jwksTtlSeconds": "5"})
    assert r.returncode != 0 and "jwksTtlSeconds" in r.stderr


def test_k7_api_rbac_can_patch_key_store_and_manage_tenants() -> None:
    docs = render(_sets())
    keys = find(docs, "Role", "k7-api-keys")["rules"]
    assert {
        "resourceNames": ["k7-api-keys"],
        "apiGroups": [""],
        "resources": ["secrets"],
        "verbs": ["get", "patch"],
    } in keys
    rules = find(docs, "ClusterRole", "k7-api")["rules"]
    tenant_verbs = ["get", "list", "watch", "create", "update", "patch", "delete"]
    assert {"apiGroups": ["k7.katakate.org"], "resources": ["tenants"], "verbs": tenant_verbs} in rules


# --- hooks, uninstall and value plumbing -------------------------------------


def _hook(doc: dict[str, Any]) -> tuple[str, int]:
    ann = doc["metadata"].get("annotations") or {}
    return ann.get("helm.sh/hook", ""), int(ann.get("helm.sh/hook-weight", "0"))


def test_hook_phases_and_order() -> None:
    docs = render({**_sets(), "hookTimeoutSeconds": "600"})
    hooks = {(d["kind"], d["metadata"]["name"]): _hook(d) for d in docs if _hook(d)[0]}
    rbac = {k: v for k, v in hooks.items() if k[1] == "k7-hook"}
    assert {k[0] for k in rbac} == {"ServiceAccount", "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding"}
    assert set(rbac.values()) == {("pre-install,pre-upgrade,pre-delete", -20)}
    assert hooks[("Job", "k7-preflight")] == ("pre-install,pre-upgrade", 0)
    pre_delete = sorted((w, n) for (_, n), (h, w) in hooks.items() if "pre-delete" in h and n != "k7-hook")
    assert pre_delete == [(-10, "k7-uninstall-stop"), (0, "k7-node-cleanup"), (10, "k7-uninstall-wait")]
    for name, command in (
        ("k7-preflight", "install"),
        ("k7-uninstall-stop", "uninstall-stop"),
        ("k7-uninstall-wait", "uninstall-wait"),
    ):
        job = find(docs, "Job", name)
        assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 720
        (c,) = job["spec"]["template"]["spec"]["containers"]
        assert c["command"] == ["python", "-m", "k7.api.chart_hooks", command]
        env = {e["name"]: e.get("value") for e in c["env"]}
        assert env["K7_HOOK_PROVIDER"] == "cilium"
        assert env["K7_HOOK_BACKENDS"] == "kql,k7d"
        assert env["K7_HOOK_TIMEOUT_S"] == "600"
        assert job["spec"]["template"]["spec"]["serviceAccountName"] == "k7-hook"
    test_pod = find(docs, "Pod", "k7-test-sandboxes")
    assert _hook(test_pod) == ("test", 0)
    (c,) = test_pod["spec"]["containers"]
    assert c["command"] == ["python", "-m", "k7.api.chart_hooks", "test"]
    assert {e["name"]: e.get("value") for e in c["env"]}["K7_HOOK_BACKENDS"] == "kql,k7d"


@pytest.mark.parametrize(("force", "args"), [("false", ["cleanup"]), ("true", ["cleanup", "--force"])])
def test_force_cleanup_reaches_the_cleanup_daemonset(force: str, args: list[str]) -> None:
    ds = find(render({**_sets(), "nodeInstaller.forceCleanup": force}), "DaemonSet", "k7-node-cleanup")
    (c,) = ds["spec"]["template"]["spec"]["containers"]
    assert c["args"] == args


def test_installer_config_carries_disk_options() -> None:
    sets = {
        **_sets(backends=("kfd", "k7d")),
        "nodeInstaller.wipeDisks": "true",
        "nodeInstaller.k7dDisksImageSize": "64G",
        "nodeInstaller.kataThinpoolPvSize": "400G",
    }
    cfg = json.loads(find(render(sets), "ConfigMap", "k7-node-installer")["data"]["config.json"])
    assert cfg["wipeDisks"] is True
    assert (cfg["k7dDisksImageSize"], cfg["kataThinpoolPvSize"]) == ("64G", "400G")
    entrypoint.validate_config(cfg)
    default = json.loads(find(render(_sets()), "ConfigMap", "k7-node-installer")["data"]["config.json"])
    assert default["wipeDisks"] is False and "k7dDisksImageSize" not in default


def test_sandbox_node_tolerations_reach_every_node_daemonset() -> None:
    tol = [{"key": "k7.katakate.org/sandbox", "operator": "Exists", "effect": "NoSchedule"}]
    docs = render({**_sets(), "sandboxNodes.tolerations": tol})
    for name in ("k7-node-installer", "k7-node-cleanup", "k7-agent"):
        spec = find(docs, "DaemonSet", name)["spec"]["template"]["spec"]
        assert tol[0] in spec["tolerations"], name


def test_cert_manager_mode_renders_a_certificate_for_the_tls_secret() -> None:
    docs = render(
        {**_sets(tls="certManager"), "tls.secretName": "my-tls", "tls.certManager.ipAddresses": ["192.0.2.10"]}
    )
    cert = find(docs, "Certificate", "k7-api")["spec"]
    assert cert == {
        "secretName": "my-tls",
        "issuerRef": {"name": "k7-ca", "kind": "ClusterIssuer", "group": "cert-manager.io"},
        "dnsNames": ["k7-api.example.com"],
        "ipAddresses": ["192.0.2.10"],
    }
    assert not [d for d in render(_sets()) if d["kind"] == "Certificate"]


def test_insecure_http_has_no_caddy_and_serves_8000() -> None:
    docs = render(_sets(tls="insecureHTTP"))
    pod = find(docs, "Deployment", "k7-api")["spec"]["template"]["spec"]
    assert [c["name"] for c in pod["containers"]] == ["k7-api"]
    assert find(docs, "Service", "k7-api")["spec"]["ports"][0]["targetPort"] == 8000
    assert not [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "k7-api-caddyfile"]


@pytest.mark.parametrize(
    ("sets", "want"),
    [
        ({}, {"type": "ClusterIP"}),
        ({"api.service.type": "NodePort", "api.service.nodePort": "30443"}, {"type": "NodePort", "nodePort": 30443}),
        ({"api.service.type": "LoadBalancer"}, {"type": "LoadBalancer"}),
        (
            {"api.service.type": "NodePort", "networkPolicy.apiAllowCIDRs": ["198.51.100.0/24"]},
            {"type": "NodePort", "externalTrafficPolicy": "Local"},
        ),
    ],
)
def test_api_service_exposure(sets: dict[str, Any], want: dict[str, Any]) -> None:
    svc = find(render({**_sets(), **sets}), "Service", "k7-api")["spec"]
    port = svc["ports"][0]
    assert port["targetPort"] == 8443
    got = {"type": svc["type"], **({"nodePort": port["nodePort"]} if "nodePort" in port else {})}
    if "externalTrafficPolicy" in svc:
        got["externalTrafficPolicy"] = svc["externalTrafficPolicy"]
    assert got == want


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"api.service.nodePort": "30443"}, "only valid with api.service.type=NodePort"),
        ({"networkPolicy.apiAllowCIDRs": ["198.51.100.0/24"]}, "needs api.service.type NodePort or LoadBalancer"),
        ({"backends.kfd.enabled": "true", "nodeInstaller.diskSource": "loopback"}, "provisions none"),
        ({"tls.mode": "certManager", "tls.certManager.dnsNames": []}, "dnsNames or tls.certManager.ipAddresses"),
    ],
)
def test_render_refuses_more(change: dict[str, Any], error: str) -> None:
    r = _template({**_sets(), **change})
    assert r.returncode != 0, f"rendered despite {change}"
    assert error in r.stderr, r.stderr


@pytest.mark.parametrize(("replicas", "budget"), [("1", {"maxUnavailable": 1}), ("3", {"minAvailable": 1})])
def test_api_replicas_and_disruption_budget(replicas: str, budget: dict[str, int]) -> None:
    docs = render({**_sets(), "api.replicas": replicas})
    assert find(docs, "Deployment", "k7-api")["spec"]["replicas"] == int(replicas)
    spec = find(docs, "PodDisruptionBudget", "k7-api")["spec"]
    assert {k: v for k, v in spec.items() if k != "selector"} == budget


def test_kql_storage_env_reaches_every_core_pod() -> None:
    docs = render({**_sets(), "backends.kql.storageClass": "sc-a", "backends.kql.volumeSnapshotClass": "vsc-a"})
    for kind, name in (
        ("Deployment", "k7-api"),
        ("CronJob", "k7-snapshot-gc"),
        ("Pod", "k7-test-sandboxes"),
        ("Job", "k7-preflight"),
    ):
        env = {
            n["name"]: n.get("value")
            for n in _walk(find(docs, kind, name))
            if str(n.get("name", "")).startswith("K7_KQL_")
        }
        assert env == {"K7_KQL_STORAGE_CLASS": "sc-a", "K7_KQL_SNAPSHOT_CLASS": "vsc-a"}, (kind, name)


def test_cilium_dns_proxy_answers_denied_names_with_nxdomain() -> None:
    """Fresh installs, existing installs (no reinstall of Cilium) and the chart's
    documented Cilium install all set dnsRejectResponseCode=nameError."""
    tasks = {t["name"]: t for t in yaml.safe_load((DEPLOY / "roles/k7_cluster/tasks/cilium.yml").read_text())}
    install = tasks["Install Cilium (kube-proxy replacement, FQDN support)"]["ansible.builtin.shell"]
    assert "--set dnsProxy.dnsRejectResponseCode=nameError" in install
    upgrade = tasks["Set Cilium DNS proxy reject code to NXDOMAIN (existing install)"]
    assert (
        'cilium upgrade --version "$version" --set dnsProxy.dnsRejectResponseCode=nameError'
        in upgrade["ansible.builtin.shell"]
    )
    assert 'cilium_dns_reject.stdout != "nameError"' in upgrade["when"]
    assert 'cilium_presence.stdout == "present"' in upgrade["when"]
    doc = (REPO / "deploy/helm/k7/ci/rke2-node.sh").read_text()
    assert "--set dnsProxy.dnsRejectResponseCode=nameError" in doc
