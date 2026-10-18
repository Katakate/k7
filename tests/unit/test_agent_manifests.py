"""Agent must not inherit k7-api ClusterRole or accept remote-node ingress."""

from pathlib import Path

MANIFESTS = Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api"


def test_agent_daemonset_has_no_api_rbac():
    ds = (MANIFESTS / "agent-daemonset.yaml").read_text()
    assert "serviceAccountName: k7-agent" in ds
    assert "automountServiceAccountToken: false" in ds
    assert "serviceAccountName: k7-api" not in ds


def test_agent_sa_has_no_clusterrolebinding():
    binding = (MANIFESTS / "clusterrolebinding.yaml").read_text()
    assert "name: k7-api" in binding
    assert "k7-agent" not in binding
    sa = (MANIFESTS / "serviceaccount.yaml").read_text()
    assert "name: k7-agent" in sa
    assert "automountServiceAccountToken: false" in sa


def test_agent_cnp_denies_remote_node():
    cnp = (MANIFESTS / "cilium/agent-networkpolicy.yaml").read_text()
    spec = cnp.split("spec:", 1)[1]
    assert "app: k7-api" in spec
    assert "- host" in spec
    assert "remote-node" not in spec


def _container(name: str) -> tuple[dict, dict]:
    import yaml

    spec = yaml.safe_load((MANIFESTS / name).read_text())["spec"]["template"]["spec"]
    return spec, spec["containers"][0]


def test_agent_is_unprivileged():
    spec, c = _container("agent-daemonset.yaml")
    sc = c["securityContext"]
    assert sc["privileged"] is False and sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["capabilities"] == {"drop": ["ALL"]}
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
    host = {v["hostPath"]["path"] for v in spec["volumes"] if "hostPath" in v}
    assert host == {"/run/k7d", "__K7_CONTAINERD_SOCKET_DIR__", "/var/lib/k7d", "/run/k7", "/var/lib/k7/usage-spool"}
    mounts = {m["name"]: m for m in c["volumeMounts"]}
    assert mounts["usage-spool"] == {"name": "usage-spool", "mountPath": "/var/lib/k7/usage-spool"}
    assert mounts["metering-headers"]["readOnly"] is True
    assert next(v["secret"] for v in spec["volumes"] if v["name"] == "metering-headers") == {
        "secretName": "k7-metering-headers",
        "optional": True,
        "defaultMode": 0o400,
    }
    assert next(v["secret"] for v in spec["volumes"] if v["name"] == "k7-config") == {
        "secretName": "k7-agent-token",
        "defaultMode": 0o400,
    }


def test_api_runs_as_nonroot_without_host_mounts():
    spec, c = _container("deployment.yaml")
    assert not any("hostPath" in v for v in spec["volumes"])
    sc = c["securityContext"]
    assert sc["runAsUser"] == 1000 and sc.get("runAsNonRoot", spec.get("securityContext", {}).get("runAsNonRoot"))
    assert sc["capabilities"] == {"drop": ["ALL"]} and sc["readOnlyRootFilesystem"] is True
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["K7_K7D_REMOTE_ONLY"] == "1" and env["K7_API_KEY_STORE"] == "secret"


def test_agent_k7d_group_is_the_gid_node_prep_pins():
    import yaml

    deploy = MANIFESTS.parents[1]
    gid = yaml.safe_load((deploy / "k7-install-node.yaml").read_text())[0]["vars"]["k7d_gid"]
    spec, _ = _container("agent-daemonset.yaml")
    assert spec["securityContext"] == {"supplementalGroups": [gid]}
    tasks = {t["name"]: t for t in yaml.safe_load((deploy / "roles/k7_node_prep/tasks/k7d.yml").read_text())}
    names = list(tasks)
    create = tasks["K7d — create the k7d group with the pinned gid"]
    assert create["ansible.builtin.group"] == {"name": "k7d", "gid": "{{ k7d_gid }}", "system": True}
    mismatch = tasks["K7d — fail loud when the k7d group has another gid than k7d_gid"]
    assert "k7d_group.stdout.split(':')[2] | int != k7d_gid | int" in mismatch["when"]
    assert names.index("K7d — read the k7d group") < names.index(
        "K7d — fail loud when the k7d group has another gid than k7d_gid"
    )
    assert names.index(create["name"]) < names.index(
        "K7d — run the release install.sh (binaries, guest assets, k7d.service)"
    )
    assert "first-boot" not in create["tags"]  # the Packer bake pins it too


def test_api_containers_drain_before_exit():
    import yaml

    from k7.deploy.k7_api_tls import render_deployment

    dep = render_deployment((MANIFESTS / "deployment.yaml").read_text(), acme=False)
    for c in yaml.safe_load(dep)["spec"]["template"]["spec"]["containers"]:
        assert c["lifecycle"]["preStop"] == {"sleep": {"seconds": 5}}, c["name"]
